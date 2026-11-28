package controller

import (
	"testing"

	"github.com/stretchr/testify/require"
	corev1 "k8s.io/api/core/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	discoverylisters "k8s.io/client-go/listers/discovery/v1"
	"k8s.io/client-go/tools/cache"

	fabricv1 "github.com/cloudyfolks-io/fabric/pkg/apis/fabric/v1"
	"github.com/cloudyfolks-io/fabric/pkg/util"
)

func TestClaimedEndpointSliceIgnoresLegacyIngress(t *testing.T) {
	class := util.LoadBalancerClass
	svc := makeLbSvc("default", "pool", &class, corev1.ServiceTypeLoadBalancer)
	svc.UID = "current-service"
	svc.Annotations = map[string]string{
		util.RouterLBRuleVipsAnnotation: "192.0.2.10",
		util.VpcAnnotation:              util.DefaultVpc,
		util.ServiceHealthCheck:         "false",
	}
	svc.Spec.ClusterIPs = []string{"10.96.0.10"}
	svc.Spec.Ports = []corev1.ServicePort{{Name: "http", Port: 80, Protocol: corev1.ProtocolTCP}}
	svc.Status.LoadBalancer.Ingress = []corev1.LoadBalancerIngress{{IP: "192.0.2.20"}}
	vpc := &fabricv1.Vpc{ObjectMeta: metav1.ObjectMeta{Name: util.DefaultVpc}, Status: fabricv1.VpcStatus{TCPLoadBalancer: "tcp"}}
	fc, err := newFakeControllerWithOptions(t, &FakeControllerOptions{Services: []*corev1.Service{svc}, Vpcs: []*fabricv1.Vpc{vpc}})
	require.NoError(t, err)
	c := fc.fakeController
	c.config.EnableLb, c.config.EnableOvnLbSvc = true, true
	port := int32(8080)
	portName := "http"
	indexer := cache.NewIndexer(cache.MetaNamespaceKeyFunc, cache.Indexers{cache.NamespaceIndex: cache.MetaNamespaceIndexFunc})
	current := &discoveryv1.EndpointSlice{
		ObjectMeta: metav1.ObjectMeta{
			Name:            "pool-ends",
			Namespace:       "default",
			Labels:          map[string]string{discoveryv1.LabelServiceName: "pool"},
			OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(svc, corev1.SchemeGroupVersion.WithKind("Service"))},
		},
		AddressType: discoveryv1.AddressTypeIPv4,
		Ports:       []discoveryv1.EndpointPort{{Name: &portName, Port: &port}},
		Endpoints:   []discoveryv1.Endpoint{{Addresses: []string{"10.0.0.2"}}},
	}
	require.NoError(t, indexer.Add(current))
	stale := current.DeepCopy()
	stale.Name = "previous-pool-ends"
	stale.OwnerReferences[0].UID = "previous-service"
	stale.Endpoints[0].Addresses = []string{"10.0.0.3"}
	require.NoError(t, indexer.Add(stale))
	c.endpointSlicesLister = discoverylisters.NewEndpointSliceLister(indexer)
	fc.mockOvnClient.EXPECT().LoadBalancerAddVip("tcp", "192.0.2.10:80", "10.0.0.2:8080").Return(nil)
	fc.mockOvnClient.EXPECT().LoadBalancerAddVip("tcp", "10.96.0.10:80", "10.0.0.2:8080").Return(nil)
	require.NoError(t, c.handleUpdateEndpointSlice("default/pool"))
}
