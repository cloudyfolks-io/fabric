package controller

import (
	"context"
	"fmt"
	"reflect"
	"slices"

	v1 "k8s.io/api/core/v1"
	k8serrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/client-go/tools/cache"
	"k8s.io/klog/v2"

	fabricv1 "github.com/cloudyfolks-io/fabric/pkg/apis/fabric/v1"
	"github.com/cloudyfolks-io/fabric/pkg/util"
)

type vpcService struct {
	Vips     []string
	Vpc      string
	Protocol v1.Protocol
	Svc      *v1.Service
}

type updateSvcObject struct {
	key      string
	previous *v1.Service
}

type serviceVIP struct {
	vpc      string
	protocol v1.Protocol
	socket   string
	session  bool
}

func (c *Controller) enqueueAddService(obj any) {
	svc := obj.(*v1.Service)
	key := cache.MetaObjectToName(svc).String()

	if c.config.EnableLb {
		klog.V(3).Infof("enqueue add service %s", key)
		c.addOrUpdateEndpointSliceQueue.Add(key)
	}

	if c.ovnLbSvcEnabled() && svc.Spec.Type == v1.ServiceTypeLoadBalancer {
		klog.V(3).Infof("enqueue add ovn lb service %s", key)
		c.addOrUpdateOvnLbSvcQueue.Add(key)
	}
}

func (c *Controller) enqueueDeleteService(obj any) {
	var svc *v1.Service
	switch t := obj.(type) {
	case *v1.Service:
		svc = t
	case cache.DeletedFinalStateUnknown:
		s, ok := t.Obj.(*v1.Service)
		if !ok {
			klog.Warningf("unexpected object type: %T", t.Obj)
			return
		}
		svc = s
	default:
		klog.Warningf("unexpected type: %T", obj)
		return
	}

	if c.ovnLbSvcEnabled() && isOvnLbSvc(svc, c.config.DefaultLoadBalancerClass) {
		klog.Infof("enqueue release ovn lb service %s/%s", svc.Namespace, svc.Name)
		c.delOvnLbSvcQueue.Add(newOvnLbSvcRelease(svc, c.config.ClusterRouter))
		// claimed services are fully owned by the ovn-lb-svc path; the
		// legacy path mirrors status ingress and cluster IPs into the same
		// LB rows and would fight the pool allocation (or resurrect VIPs the
		// pool has just released).
		return
	}

	if !c.config.EnableLb {
		return
	}

	klog.Infof("enqueue delete service %s/%s", svc.Namespace, svc.Name)

	ips := getVipIps(svc)
	if len(ips) == 0 {
		return
	}
	for _, vpc := range serviceVIPScopes(svc) {
		if vpc == "" {
			vpc = c.config.ClusterRouter
		}
		for _, port := range svc.Spec.Ports {
			vpcSvc := &vpcService{
				Protocol: port.Protocol,
				Vpc:      vpc,
				Svc:      svc,
			}
			for _, ip := range ips {
				vpcSvc.Vips = append(vpcSvc.Vips, util.JoinHostPort(ip, port.Port))
			}
			klog.V(3).Infof("delete vpc service: %v", vpcSvc)
			c.deleteServiceQueue.Add(vpcSvc)
		}
	}
}

func (c *Controller) enqueueUpdateService(oldObj, newObj any) {
	oldSvc := oldObj.(*v1.Service)
	newSvc := newObj.(*v1.Service)
	if oldSvc.ResourceVersion == newSvc.ResourceVersion {
		return
	}

	if c.ovnLbSvcEnabled() {
		oldClaimed := isOvnLbSvc(oldSvc, c.config.DefaultLoadBalancerClass)
		newClaimed := isOvnLbSvc(newSvc, c.config.DefaultLoadBalancerClass)
		if oldClaimed && !newClaimed {
			klog.Infof("enqueue release ovn lb service %s/%s", oldSvc.Namespace, oldSvc.Name)
			c.delOvnLbSvcQueue.Add(newOvnLbSvcRelease(oldSvc, c.config.ClusterRouter))
		}
		if newClaimed {
			c.addOrUpdateOvnLbSvcQueue.Add(cache.MetaObjectToName(newSvc).String())
			// claimed services are fully owned by the ovn-lb-svc path; the
			// legacy path would mirror cluster IPs and status ingress into
			// the same LB rows and fight the pool allocation. Services
			// migrating away from the claim (oldClaimed && !newClaimed)
			// still fall through so the legacy path can release anything the
			// legacy status carried.
			return
		}
	}

	if !c.config.EnableLb {
		return
	}

	key := cache.MetaObjectToName(newSvc).String()
	if newSvc.Annotations[util.SwitchLBRuleVipsAnnotation] != "" &&
		serviceHealthChecksDisabled(oldSvc) != serviceHealthChecksDisabled(newSvc) {
		c.addOrUpdateEndpointSliceQueue.Add(key)
	}

	oldClusterIps := getVipIps(oldSvc)
	newClusterIps := getVipIps(newSvc)

	if newSvc.Spec.Type != v1.ServiceTypeLoadBalancer &&
		oldSvc.DeletionTimestamp.Equal(newSvc.DeletionTimestamp) &&
		oldSvc.Annotations[util.VpcAnnotation] == newSvc.Annotations[util.VpcAnnotation] &&
		oldSvc.Annotations[util.LogicalRouterAnnotation] == newSvc.Annotations[util.LogicalRouterAnnotation] &&
		slices.Equal(oldClusterIps, newClusterIps) &&
		reflect.DeepEqual(oldSvc.Spec, newSvc.Spec) {
		return
	}

	klog.V(3).Infof("enqueue update service %s", key)
	c.updateServiceQueue.Add(&updateSvcObject{key: key, previous: oldSvc})
	if c.config.EnableOVNLBPreferLocal &&
		newSvc.Spec.Type == v1.ServiceTypeLoadBalancer &&
		oldSvc.Spec.ExternalTrafficPolicy != newSvc.Spec.ExternalTrafficPolicy {
		c.addOrUpdateEndpointSliceQueue.Add(cache.MetaObjectToName(newSvc).String())
	}
}

func (c *Controller) handleDeleteService(service *vpcService) error {
	key := cache.MetaObjectToName(service.Svc).String()

	c.svcKeyMutex.LockKey(key)
	defer func() { _ = c.svcKeyMutex.UnlockKey(key) }()
	klog.Infof("handle delete service %s", key)

	vips := make([]serviceVIP, 0, len(service.Vips))
	for _, socket := range service.Vips {
		vips = append(vips, serviceVIP{vpc: service.Vpc, protocol: service.Protocol, socket: socket})
	}
	return c.removeUnusedServiceVIPs(service.Vpc, vips)
}

func (c *Controller) removeUnusedServiceVIPs(vpcName string, vips []serviceVIP) error {
	if vpcName == "" || len(vips) == 0 {
		return nil
	}
	vpc, err := c.vpcsLister.Get(vpcName)
	if k8serrors.IsNotFound(err) {
		return nil
	}
	if err != nil {
		return err
	}

	services, err := c.servicesLister.List(labels.Everything())
	if err != nil {
		return err
	}
	active := activeServiceVIPs(services)
	recorded := recordedServiceLoadBalancers(vpc)
	generated := c.GenVpcLoadBalancer(vpcName)
	for _, vip := range vips {
		fallback := serviceLoadBalancers(*generated, vip.protocol)
		for index, lb := range serviceLoadBalancers(recorded, vip.protocol) {
			vip.session = index == 1
			if serviceVIPActive(active, vip) {
				continue
			}
			if lb == "" {
				lb = fallback[index]
				if lb == "" {
					continue
				}
				observed, err := c.OVNNbClient.GetLoadBalancer(lb, true)
				if err != nil {
					return err
				}
				if observed == nil {
					continue
				}
				if _, found := observed.Vips[vip.socket]; !found {
					continue
				}
			}
			if c.config.EnableOVNLBPreferLocal {
				if err := c.OVNNbClient.LoadBalancerDeleteIPPortMapping(lb, vip.socket); err != nil {
					return err
				}
			}
			if err := c.OVNNbClient.LoadBalancerDeleteVip(lb, vip.socket, true); err != nil {
				return err
			}
		}
	}
	return nil
}

func (c *Controller) removePreviousServiceVIPs(svc *v1.Service) error {
	for _, vpc := range serviceVIPScopes(svc) {
		if vpc == "" {
			vpc = c.config.ClusterRouter
		}
		if err := c.removeUnusedServiceVIPs(vpc, serviceVIPs(svc, vpc)); err != nil {
			return err
		}
	}
	return nil
}

func (c *Controller) handleUpdateService(item *updateSvcObject) error {
	key := item.key
	namespace, name, err := cache.SplitMetaNamespaceKey(key)
	if err != nil {
		utilruntime.HandleError(fmt.Errorf("invalid resource key %s: %w", key, err))
		return nil
	}

	c.svcKeyMutex.LockKey(key)
	defer func() { _ = c.svcKeyMutex.UnlockKey(key) }()
	klog.Infof("handle update service %s", key)

	previousVIPs := serviceVIPs(item.previous, serviceVIPVpc(item.previous))
	svc, err := c.servicesLister.Services(namespace).Get(name)
	if k8serrors.IsNotFound(err) {
		return c.removePreviousServiceVIPs(item.previous)
	}
	if err != nil {
		return err
	}
	if c.ovnLbSvcEnabled() && isOvnLbSvc(svc, c.config.DefaultLoadBalancerClass) {
		return nil
	}

	vpcName := serviceVIPVpc(svc)
	vips := serviceVIPs(svc, vpcName)
	vipsChanged := !slices.Equal(previousVIPs, vips)
	routerChanged := item.previous != nil && item.previous.Annotations[util.LogicalRouterAnnotation] != svc.Annotations[util.LogicalRouterAnnotation]
	if vipsChanged || routerChanged {
		if err := c.removePreviousServiceVIPs(item.previous); err != nil {
			return err
		}
	}
	needUpdate := item.previous == nil || !reflect.DeepEqual(item.previous.Spec, svc.Spec) || routerChanged || vipsChanged
	if !needUpdate {
		if vpcName == "" {
			vpcName = c.config.ClusterRouter
		}
		vpc, err := c.vpcsLister.Get(vpcName)
		if err != nil {
			return err
		}
		desired := make(map[string][]string)
		recorded := recordedServiceLoadBalancers(vpc)
		for _, vip := range vips {
			loadBalancers := serviceLoadBalancers(recorded, vip.protocol)
			lb := loadBalancers[0]
			if vip.session {
				lb = loadBalancers[1]
			}
			if lb != "" {
				desired[lb] = append(desired[lb], vip.socket)
			}
		}
		for lb, sockets := range desired {
			observed, err := c.OVNNbClient.GetLoadBalancer(lb, false)
			if err != nil {
				return err
			}
			for _, socket := range sockets {
				if _, found := observed.Vips[socket]; !found {
					needUpdate = true
					break
				}
			}
		}
	}
	if err := c.checkServiceLBIPBelongToSubnet(svc); err != nil {
		return err
	}
	if needUpdate {
		c.addOrUpdateEndpointSliceQueue.Add(key)
	}
	if vip := svc.Annotations[util.SwitchLBRuleVipsAnnotation]; vip != "" {
		rules, err := c.switchLBRuleLister.List(labels.Everything())
		if err != nil {
			return err
		}
		for _, rule := range rules {
			if rule.Spec.Vip == vip {
				c.addOrUpdateEndpointSliceQueue.Add(fmt.Sprintf("%s/slr-%s", rule.Spec.Namespace, rule.Name))
			}
		}
	}
	return nil
}

func serviceVIPVpc(svc *v1.Service) string {
	if svc == nil {
		return ""
	}
	if vpc := svc.Annotations[util.VpcAnnotation]; vpc != "" {
		return vpc
	}
	return svc.Annotations[util.LogicalRouterAnnotation]
}

func serviceVIPScopes(svc *v1.Service) []string {
	if svc == nil {
		return nil
	}
	vpc := serviceVIPVpc(svc)
	if declared := svc.Annotations[util.LogicalRouterAnnotation]; declared != "" && declared != vpc {
		return []string{vpc, declared}
	}
	return []string{vpc}
}

func serviceVIPs(svc *v1.Service, vpc string) []serviceVIP {
	if svc == nil {
		return nil
	}
	ips := getVipIps(svc)
	vips := make([]serviceVIP, 0, len(svc.Spec.Ports)*len(ips))
	for _, port := range svc.Spec.Ports {
		for _, ip := range ips {
			vips = append(vips, serviceVIP{
				vpc: vpc, protocol: port.Protocol, socket: util.JoinHostPort(ip, port.Port),
				session: svc.Spec.SessionAffinity == v1.ServiceAffinityClientIP,
			})
		}
	}
	return vips
}

func activeServiceVIPs(services []*v1.Service) map[serviceVIP]struct{} {
	active := make(map[serviceVIP]struct{})
	for _, svc := range services {
		for _, vpc := range serviceVIPScopes(svc) {
			for _, vip := range serviceVIPs(svc, vpc) {
				active[vip] = struct{}{}
			}
		}
	}
	return active
}

func serviceVIPActive(active map[serviceVIP]struct{}, vip serviceVIP) bool {
	if _, found := active[vip]; found {
		return true
	}
	vip.vpc = ""
	_, found := active[vip]
	return found
}

func recordedServiceLoadBalancers(vpc *fabricv1.Vpc) VpcLoadBalancer {
	return VpcLoadBalancer{
		TCPLoadBalancer: vpc.Status.TCPLoadBalancer, TCPSessLoadBalancer: vpc.Status.TCPSessionLoadBalancer,
		UDPLoadBalancer: vpc.Status.UDPLoadBalancer, UDPSessLoadBalancer: vpc.Status.UDPSessionLoadBalancer,
		SctpLoadBalancer: vpc.Status.SctpLoadBalancer, SctpSessLoadBalancer: vpc.Status.SctpSessionLoadBalancer,
	}
}

func serviceLoadBalancers(lbs VpcLoadBalancer, protocol v1.Protocol) [2]string {
	switch protocol {
	case v1.ProtocolTCP:
		return [2]string{lbs.TCPLoadBalancer, lbs.TCPSessLoadBalancer}
	case v1.ProtocolUDP:
		return [2]string{lbs.UDPLoadBalancer, lbs.UDPSessLoadBalancer}
	case v1.ProtocolSCTP:
		return [2]string{lbs.SctpLoadBalancer, lbs.SctpSessLoadBalancer}
	default:
		return [2]string{}
	}
}

func serviceAnnotationVips(svc *v1.Service) []string {
	vips := splitAnnotationIPs(svc.Annotations[util.SwitchLBRuleVipsAnnotation])
	return append(vips, splitAnnotationIPs(svc.Annotations[util.RouterLBRuleVipsAnnotation])...)
}

func serviceIngressIPs(svc *v1.Service) []string {
	if svc.Annotations[util.AttachmentProvider] != "" {
		return nil
	}
	var ips []string
	for _, ingress := range svc.Status.LoadBalancer.Ingress {
		if ingress.IP != "" {
			ips = append(ips, ingress.IP)
		}
	}
	return ips
}

func getVipIps(svc *v1.Service) []string {
	var ips []string
	for _, group := range [][]string{serviceAnnotationVips(svc), util.ServiceClusterIPs(*svc), serviceIngressIPs(svc)} {
		for _, ip := range group {
			if !slices.Contains(ips, ip) {
				ips = append(ips, ip)
			}
		}
	}
	return ips
}

func (c *Controller) checkServiceLBIPBelongToSubnet(svc *v1.Service) error {
	desiredSubnet := ""
	if len(svc.Status.LoadBalancer.Ingress) > 0 {
		subnets, err := c.subnetsLister.List(labels.Everything())
		if err != nil {
			klog.Errorf("failed to list subnets: %v", err)
			return err
		}
		for _, subnet := range subnets {
			for _, ingress := range svc.Status.LoadBalancer.Ingress {
				if ingress.IP == "" {
					continue
				}
				if util.CIDRContainIP(subnet.Spec.CIDRBlock, ingress.IP) {
					desiredSubnet = subnet.Name
					break
				}
			}
		}
	}

	cur, ok := svc.Annotations[util.ServiceExternalIPFromSubnetAnnotation]
	if desiredSubnet == "" {
		if !ok {
			return nil
		}
	} else if cur == desiredSubnet {
		return nil
	}

	newSvc := svc.DeepCopy()
	if desiredSubnet != "" {
		if newSvc.Annotations == nil {
			newSvc.Annotations = map[string]string{}
		}
		newSvc.Annotations[util.ServiceExternalIPFromSubnetAnnotation] = desiredSubnet
	} else {
		delete(newSvc.Annotations, util.ServiceExternalIPFromSubnetAnnotation)
	}

	klog.Infof("update service %s/%s external IP subnet annotation: %q -> %q",
		svc.Namespace, svc.Name, cur, desiredSubnet)
	if _, err := c.config.KubeClient.CoreV1().Services(svc.Namespace).Update(context.TODO(), newSvc, metav1.UpdateOptions{}); err != nil {
		klog.Errorf("failed to update service %s/%s: %v", svc.Namespace, svc.Name, err)
		return err
	}

	return nil
}
