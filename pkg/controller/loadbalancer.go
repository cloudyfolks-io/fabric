package controller

import (
	"context"
	"fmt"

	apiequality "k8s.io/apimachinery/pkg/api/equality"
	k8serrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/cache"
	"k8s.io/klog/v2"

	fabricv1 "github.com/cloudyfolks-io/fabric/pkg/apis/fabric/v1"
	"github.com/cloudyfolks-io/fabric/pkg/util"
)

type loadBalancerDeletion struct {
	Name string
	UID  types.UID
}

func lbChildName(name string) string {
	return "flb-" + name
}

func (c *Controller) enqueueAddLoadBalancer(obj any) {
	key := cache.MetaObjectToName(obj.(*fabricv1.LoadBalancer)).String()
	klog.Infof("enqueue add LoadBalancer %s", key)
	c.addLoadBalancerQueue.Add(key)
}

func (c *Controller) enqueueUpdateLoadBalancer(oldObj, newObj any) {
	oldLb := oldObj.(*fabricv1.LoadBalancer)
	newLb := newObj.(*fabricv1.LoadBalancer)
	if oldLb.ResourceVersion == newLb.ResourceVersion {
		return
	}
	c.addLoadBalancerQueue.Add(newLb.Name)
}

func (c *Controller) enqueueDeleteLoadBalancer(obj any) {
	var lb *fabricv1.LoadBalancer
	switch t := obj.(type) {
	case *fabricv1.LoadBalancer:
		lb = t
	case cache.DeletedFinalStateUnknown:
		l, ok := t.Obj.(*fabricv1.LoadBalancer)
		if !ok {
			klog.Warningf("unexpected object type: %T", t.Obj)
			return
		}
		lb = l
	default:
		klog.Warningf("unexpected type: %T", obj)
		return
	}
	klog.Infof("enqueue del LoadBalancer %s", lb.Name)
	c.delLoadBalancerQueue.Add(loadBalancerDeletion{Name: lb.Name, UID: lb.UID})
}

func (c *Controller) enqueueLoadBalancerForChild(obj any) {
	if unknown, ok := obj.(cache.DeletedFinalStateUnknown); ok {
		obj = unknown.Obj
	}
	accessor, err := meta.Accessor(obj)
	if err != nil {
		return
	}
	if owner := accessor.GetLabels()[util.LoadBalancerOwnerLabel]; owner != "" {
		c.addLoadBalancerQueue.Add(owner)
	}
}

func (c *Controller) handleAddOrUpdateLoadBalancer(name string) error {
	klog.V(3).Infof("handleAddOrUpdateLoadBalancer %s", name)
	lb, err := c.config.FabricClient.FabricV1().LoadBalancers().Get(context.Background(), name, metav1.GetOptions{})
	if err != nil {
		if k8serrors.IsNotFound(err) {
			return nil
		}
		klog.Error(err)
		return err
	}

	if lb.DeletionTimestamp != nil {
		return nil
	}

	hasVip, hasEip := lb.Spec.Frontend.Vip != "", lb.Spec.Frontend.OvnEip != ""
	if hasVip == hasEip {
		err := fmt.Errorf("LoadBalancer %s: exactly one of frontend.vip and frontend.ovnEip must be set", name)
		klog.Error(err)
		return c.patchLoadBalancerStatus(lb, "", "ValidateFailed", err.Error())
	}

	child := lbChildName(lb.Name)
	childMeta := metav1.ObjectMeta{
		Name:            child,
		Labels:          map[string]string{util.LoadBalancerOwnerLabel: lb.Name},
		OwnerReferences: []metav1.OwnerReference{generatedFrontendOwner(lb, "LoadBalancer")},
	}

	if hasVip {
		if err := c.deleteLoadBalancerChildRouterRule(lb, child); err != nil {
			return err
		}
		vip := lb.Spec.Frontend.Vip

		var childAnnotations map[string]string
		if lb.Spec.Vpc != "" {
			childAnnotations = map[string]string{util.LogicalRouterAnnotation: lb.Spec.Vpc}
		}
		if health := lb.Annotations[util.ServiceHealthCheck]; health != "" {
			if childAnnotations == nil {
				childAnnotations = map[string]string{}
			}
			childAnnotations[util.ServiceHealthCheck] = health
		}
		childMeta.Annotations = childAnnotations
		desired := &fabricv1.SwitchLBRule{
			ObjectMeta: childMeta,
			Spec: fabricv1.SwitchLBRuleSpec{
				Vip:             vip,
				Namespace:       lb.Spec.Namespace,
				Selector:        lb.Spec.Selector,
				Endpoints:       lb.Spec.Endpoints,
				SessionAffinity: lb.Spec.SessionAffinity,
				Ports:           toSwitchLBRulePorts(lb.Spec.Ports),
			},
		}
		if err := c.upsertLoadBalancerChildSwitchRule(lb, desired); err != nil {
			return err
		}
		return c.syncLoadBalancerStatusFromSwitchRule(lb, child, vip)
	}

	if err := c.deleteLoadBalancerChildSwitchRule(lb, child); err != nil {
		return err
	}
	desired := &fabricv1.RouterLBRule{
		ObjectMeta: childMeta,
		Spec: fabricv1.RouterLBRuleSpec{
			OvnEip:          lb.Spec.Frontend.OvnEip,
			Vpc:             lb.Spec.Vpc,
			Namespace:       lb.Spec.Namespace,
			Selector:        lb.Spec.Selector,
			Endpoints:       lb.Spec.Endpoints,
			SessionAffinity: lb.Spec.SessionAffinity,
			Ports:           toRouterLBRulePorts(lb.Spec.Ports),
		},
	}
	if err := c.upsertLoadBalancerChildRouterRule(lb, desired); err != nil {
		return err
	}
	return c.syncLoadBalancerStatusFromRouterRule(lb, child)
}

func (c *Controller) syncLoadBalancerStatusFromRouterRule(lb *fabricv1.LoadBalancer, child string) error {
	vip := ""
	if eip, err := c.ovnEipsLister.Get(lb.Spec.Frontend.OvnEip); err == nil {
		vip = eip.Status.V4Ip
	}
	rule, err := c.routerLBRuleLister.Get(child)
	if err != nil {
		if k8serrors.IsNotFound(err) {
			return c.patchLoadBalancerStatus(lb, vip, "Translating", "child rule not observed yet")
		}
		return err
	}
	if !loadBalancerOwnsRule(lb, rule) {
		return c.patchLoadBalancerStatus(lb, "", "OwnerMismatch", "the child rule belongs to a different owner")
	}
	updated := lb.DeepCopy()
	updated.Status.Vip = vip
	updated.Status.Ports = rule.Status.Ports
	updated.Status.Service = rule.Status.Service
	if apiequality.Semantic.DeepEqual(lb.Status, updated.Status) {
		return nil
	}
	_, err = c.config.FabricClient.FabricV1().LoadBalancers().UpdateStatus(context.Background(), updated, metav1.UpdateOptions{})
	return err
}

func (c *Controller) handleDelLoadBalancer(deleted loadBalancerDeletion) error {
	klog.V(3).Infof("handleDelLoadBalancer %s", deleted.Name)
	current, err := c.config.FabricClient.FabricV1().LoadBalancers().Get(context.Background(), deleted.Name, metav1.GetOptions{})
	if err != nil && !k8serrors.IsNotFound(err) {
		return err
	}
	if err == nil && current.UID != deleted.UID {
		return nil
	}
	parent := &fabricv1.LoadBalancer{ObjectMeta: metav1.ObjectMeta{Name: deleted.Name, UID: deleted.UID}}
	child := lbChildName(deleted.Name)
	if err := c.deleteLoadBalancerChildSwitchRule(parent, child); err != nil {
		return err
	}
	return c.deleteLoadBalancerChildRouterRule(parent, child)
}

func loadBalancerOwnsRule(parent *fabricv1.LoadBalancer, child metav1.Object) bool {
	if child.GetLabels()[util.LoadBalancerOwnerLabel] != parent.Name {
		return false
	}
	if len(child.GetOwnerReferences()) == 0 {
		return true
	}
	owner := metav1.GetControllerOf(child)
	return owner != nil && owner.UID == parent.UID && owner.Name == parent.Name &&
		owner.APIVersion == fabricv1.SchemeGroupVersion.String() && owner.Kind == "LoadBalancer"
}

func (c *Controller) upsertLoadBalancerChildSwitchRule(parent *fabricv1.LoadBalancer, desired *fabricv1.SwitchLBRule) error {
	rules := c.config.FabricClient.FabricV1().SwitchLBRules()
	existing, err := rules.Get(context.Background(), desired.Name, metav1.GetOptions{})
	if k8serrors.IsNotFound(err) {
		_, err = rules.Create(context.Background(), desired, metav1.CreateOptions{})
		return err
	}
	if err != nil {
		return err
	}
	if !loadBalancerOwnsRule(parent, existing) {
		return fmt.Errorf("the SwitchLBRule %s belongs to a different owner", desired.Name)
	}
	updated := existing.DeepCopy()
	updated.Labels = desired.Labels
	updated.OwnerReferences = desired.OwnerReferences
	updated.Annotations = desired.Annotations
	updated.Spec = desired.Spec
	if apiequality.Semantic.DeepEqual(existing, updated) {
		return nil
	}
	_, err = rules.Update(context.Background(), updated, metav1.UpdateOptions{})
	return err
}

func (c *Controller) upsertLoadBalancerChildRouterRule(parent *fabricv1.LoadBalancer, desired *fabricv1.RouterLBRule) error {
	rules := c.config.FabricClient.FabricV1().RouterLBRules()
	existing, err := rules.Get(context.Background(), desired.Name, metav1.GetOptions{})
	if k8serrors.IsNotFound(err) {
		_, err = rules.Create(context.Background(), desired, metav1.CreateOptions{})
		return err
	}
	if err != nil {
		return err
	}
	if !loadBalancerOwnsRule(parent, existing) {
		return fmt.Errorf("the RouterLBRule %s belongs to a different owner", desired.Name)
	}
	updated := existing.DeepCopy()
	updated.Labels = desired.Labels
	updated.OwnerReferences = desired.OwnerReferences
	updated.Spec = desired.Spec
	if apiequality.Semantic.DeepEqual(existing, updated) {
		return nil
	}
	_, err = rules.Update(context.Background(), updated, metav1.UpdateOptions{})
	return err
}

func generatedFrontendOwner(object metav1.Object, kind string) metav1.OwnerReference {
	owner := metav1.NewControllerRef(object, fabricv1.SchemeGroupVersion.WithKind(kind))
	owner.BlockOwnerDeletion = nil
	return *owner
}

func generatedFrontendDeleteOptions(object metav1.Object) metav1.DeleteOptions {
	uid, version := object.GetUID(), object.GetResourceVersion()
	return metav1.DeleteOptions{Preconditions: &metav1.Preconditions{UID: &uid, ResourceVersion: &version}}
}

func (c *Controller) deleteLoadBalancerChildSwitchRule(parent *fabricv1.LoadBalancer, name string) error {
	rules := c.config.FabricClient.FabricV1().SwitchLBRules()
	existing, err := rules.Get(context.Background(), name, metav1.GetOptions{})
	if k8serrors.IsNotFound(err) {
		return nil
	}
	if err != nil || !loadBalancerOwnsRule(parent, existing) {
		return err
	}
	err = rules.Delete(context.Background(), name, generatedFrontendDeleteOptions(existing))
	if k8serrors.IsNotFound(err) {
		return nil
	}
	return err
}

func (c *Controller) deleteLoadBalancerChildRouterRule(parent *fabricv1.LoadBalancer, name string) error {
	rules := c.config.FabricClient.FabricV1().RouterLBRules()
	existing, err := rules.Get(context.Background(), name, metav1.GetOptions{})
	if k8serrors.IsNotFound(err) {
		return nil
	}
	if err != nil || !loadBalancerOwnsRule(parent, existing) {
		return err
	}
	err = rules.Delete(context.Background(), name, generatedFrontendDeleteOptions(existing))
	if k8serrors.IsNotFound(err) {
		return nil
	}
	return err
}

func (c *Controller) syncLoadBalancerStatusFromSwitchRule(lb *fabricv1.LoadBalancer, child, vip string) error {
	rule, err := c.switchLBRuleLister.Get(child)
	if err != nil {
		if k8serrors.IsNotFound(err) {
			return c.patchLoadBalancerStatus(lb, vip, "Translating", "child rule not observed yet")
		}
		return err
	}
	if !loadBalancerOwnsRule(lb, rule) {
		return c.patchLoadBalancerStatus(lb, "", "OwnerMismatch", "the child rule belongs to a different owner")
	}
	updated := lb.DeepCopy()
	updated.Status.Vip = vip
	updated.Status.Ports = rule.Status.Ports
	updated.Status.Service = rule.Status.Service
	if apiequality.Semantic.DeepEqual(lb.Status, updated.Status) {
		return nil
	}
	_, err = c.config.FabricClient.FabricV1().LoadBalancers().UpdateStatus(context.Background(), updated, metav1.UpdateOptions{})
	return err
}

func (c *Controller) patchLoadBalancerStatus(lb *fabricv1.LoadBalancer, vip, reason, message string) error {
	updated := lb.DeepCopy()
	updated.Status.Vip = vip
	updated.Status.Ports = ""
	updated.Status.Service = ""
	if apiequality.Semantic.DeepEqual(lb.Status, updated.Status) {
		return nil
	}
	if _, err := c.config.FabricClient.FabricV1().LoadBalancers().UpdateStatus(context.Background(), updated, metav1.UpdateOptions{}); err != nil {
		klog.Errorf("update LoadBalancer %s status: %v", lb.Name, err)
		return err
	}
	if message != "" {
		klog.V(3).Infof("LoadBalancer %s: %s: %s", lb.Name, reason, message)
	}
	return nil
}

func toSwitchLBRulePorts(ports []fabricv1.LoadBalancerPort) []fabricv1.SwitchLBRulePort {
	out := make([]fabricv1.SwitchLBRulePort, 0, len(ports))
	for _, p := range ports {
		out = append(out, fabricv1.SwitchLBRulePort(p))
	}
	return out
}

func toRouterLBRulePorts(ports []fabricv1.LoadBalancerPort) []fabricv1.RouterLBRulePort {
	out := make([]fabricv1.RouterLBRulePort, 0, len(ports))
	for _, p := range ports {
		out = append(out, fabricv1.RouterLBRulePort(p))
	}
	return out
}
