package controller

import (
	"testing"

	"github.com/stretchr/testify/require"
	"go.uber.org/mock/gomock"
	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	fabricv1 "github.com/cloudyfolks-io/fabric/pkg/apis/fabric/v1"
	"github.com/cloudyfolks-io/fabric/pkg/ovsdb/ovnnb"
	"github.com/cloudyfolks-io/fabric/pkg/util"
)

const fixtureVIP = "192.0.2.15"

func fixtureVIPService(name, vpc string, protocol v1.Protocol, port int32) *v1.Service {
	return &v1.Service{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "project", ResourceVersion: "2", Annotations: map[string]string{
			util.VpcAnnotation: vpc, util.SwitchLBRuleVipsAnnotation: fixtureVIP,
		}},
		Spec: v1.ServiceSpec{ClusterIP: v1.ClusterIPNone, Ports: []v1.ServicePort{{Protocol: protocol, Port: port}}},
	}
}

func TestServiceVIPMoveRetiresThePreviousVPCSocket(t *testing.T) {
	current := fixtureVIPService("database", "target", v1.ProtocolTCP, 6432)
	current.Annotations[util.LogicalRouterAnnotation] = "target"
	previous := current.DeepCopy()
	previous.ResourceVersion = "1"
	previous.Annotations[util.VpcAnnotation] = "legacy"
	otherPort := fixtureVIPService("other-port", "legacy", v1.ProtocolTCP, 6433)
	otherProtocol := fixtureVIPService("other-protocol", "legacy", v1.ProtocolUDP, 6432)
	otherSession := fixtureVIPService("other-session", "legacy", v1.ProtocolTCP, 6432)
	otherSession.Spec.SessionAffinity = v1.ServiceAffinityClientIP
	fixture, err := newFakeControllerWithOptions(t, &FakeControllerOptions{
		Services: []*v1.Service{current, otherPort, otherProtocol, otherSession},
		Vpcs: []*fabricv1.Vpc{
			{ObjectMeta: metav1.ObjectMeta{Name: "legacy"}, Status: fabricv1.VpcStatus{
				TCPLoadBalancer: "legacy-tcp", TCPSessionLoadBalancer: "legacy-tcp-session", UDPLoadBalancer: "legacy-udp",
			}},
			{ObjectMeta: metav1.ObjectMeta{Name: "target"}, Status: fabricv1.VpcStatus{TCPLoadBalancer: "target-tcp"}},
		},
	})
	require.NoError(t, err)
	controller := fixture.fakeController
	controller.config.EnableLb = true
	controller.svcKeyMutex = controller.podKeyMutex
	controller.updateServiceQueue = newTypedRateLimitingQueue[*updateSvcObject]("UpdateService", nil)
	controller.addOrUpdateEndpointSliceQueue = newTypedRateLimitingQueue[string]("EndpointSlice", nil)
	t.Cleanup(controller.updateServiceQueue.ShutDown)
	t.Cleanup(controller.addOrUpdateEndpointSliceQueue.ShutDown)
	fixture.mockOvnClient.EXPECT().LoadBalancerDeleteVip("legacy-tcp", "192.0.2.15:6432", true).Return(nil)
	fixture.mockOvnClient.EXPECT().GetLoadBalancer("vpc-target-tcp-sess-load", true).Return(nil, nil)

	controller.enqueueUpdateService(previous, current)
	require.Equal(t, 1, controller.updateServiceQueue.Len())
	item, shutdown := controller.updateServiceQueue.Get()
	require.False(t, shutdown)
	defer controller.updateServiceQueue.Done(item)
	require.NoError(t, controller.handleUpdateService(item))
	require.Equal(t, 1, controller.addOrUpdateEndpointSliceQueue.Len())
}

func TestServiceVIPDeletionCleansBothInterruptedMoveScopes(t *testing.T) {
	retired := fixtureVIPService("database", "legacy", v1.ProtocolTCP, 6432)
	retired.Annotations[util.LogicalRouterAnnotation] = "destination"
	overlap := fixtureVIPService("overlap", "target", v1.ProtocolTCP, 6432)
	delete(overlap.Annotations, util.SwitchLBRuleVipsAnnotation)
	overlap.Spec.ClusterIP = fixtureVIP
	fixture, err := newFakeControllerWithOptions(t, &FakeControllerOptions{
		Services: []*v1.Service{overlap},
		Vpcs: []*fabricv1.Vpc{
			{ObjectMeta: metav1.ObjectMeta{Name: "legacy"}, Status: fabricv1.VpcStatus{
				TCPLoadBalancer: "legacy-tcp", TCPSessionLoadBalancer: "legacy-tcp-session",
			}},
			{ObjectMeta: metav1.ObjectMeta{Name: "destination"}},
		},
	})
	require.NoError(t, err)
	controller := fixture.fakeController
	controller.config.EnableLb = true
	controller.svcKeyMutex = controller.podKeyMutex
	controller.deleteServiceQueue = newTypedRateLimitingQueue[*vpcService]("DeleteService", nil)
	t.Cleanup(controller.deleteServiceQueue.ShutDown)
	fixture.mockOvnClient.EXPECT().LoadBalancerDeleteVip("legacy-tcp", "192.0.2.15:6432", true).Return(nil).Times(2)
	fixture.mockOvnClient.EXPECT().LoadBalancerDeleteVip("legacy-tcp-session", "192.0.2.15:6432", true).Return(nil).Times(2)
	fixture.mockOvnClient.EXPECT().GetLoadBalancer("vpc-destination-tcp-load", true).Return(&ovnnb.LoadBalancer{Vips: map[string]string{
		"192.0.2.15:6432": "backend",
	}}, nil).Times(2)
	fixture.mockOvnClient.EXPECT().LoadBalancerDeleteVip("vpc-destination-tcp-load", "192.0.2.15:6432", true).Return(nil).Times(2)
	fixture.mockOvnClient.EXPECT().GetLoadBalancer("vpc-destination-tcp-sess-load", true).Return(nil, nil).Times(2)

	controller.enqueueDeleteService(retired)
	require.Equal(t, 2, controller.deleteServiceQueue.Len())
	for range 2 {
		item, shutdown := controller.deleteServiceQueue.Get()
		require.False(t, shutdown)
		require.NoError(t, controller.handleDeleteService(item))
		controller.deleteServiceQueue.Done(item)
	}
	require.NoError(t, controller.handleUpdateService(&updateSvcObject{key: "project/database", previous: retired}))
}

func TestServiceVIPDeletionUsesTheConfiguredDefaultForAnUnannotatedService(t *testing.T) {
	retired := fixtureVIPService("unannotated", "", v1.ProtocolTCP, 6432)
	delete(retired.Annotations, util.SwitchLBRuleVipsAnnotation)
	retired.Spec.ClusterIP = fixtureVIP
	fixture, err := newFakeControllerWithOptions(t, &FakeControllerOptions{
		Vpcs: []*fabricv1.Vpc{{ObjectMeta: metav1.ObjectMeta{Name: util.DefaultVpc}}},
	})
	require.NoError(t, err)
	controller := fixture.fakeController
	controller.config.EnableLb = true
	controller.config.ClusterTCPLoadBalancer = "custom-default-tcp"
	controller.svcKeyMutex = controller.podKeyMutex
	controller.deleteServiceQueue = newTypedRateLimitingQueue[*vpcService]("DeleteService", nil)
	t.Cleanup(controller.deleteServiceQueue.ShutDown)
	fixture.mockOvnClient.EXPECT().GetLoadBalancer("custom-default-tcp", true).Return(&ovnnb.LoadBalancer{Vips: map[string]string{
		"192.0.2.15:6432": "backend",
	}}, nil)
	fixture.mockOvnClient.EXPECT().LoadBalancerDeleteVip("custom-default-tcp", "192.0.2.15:6432", true).Return(nil)

	controller.enqueueDeleteService(retired)
	require.Equal(t, 1, controller.deleteServiceQueue.Len())
	item, shutdown := controller.deleteServiceQueue.Get()
	require.False(t, shutdown)
	defer controller.deleteServiceQueue.Done(item)
	require.NoError(t, controller.handleDeleteService(item))
}

func TestLoadBalancerGCScopesVIPsByVPCAndPreservesPendingMoves(t *testing.T) {
	current := fixtureVIPService("moved", "target", v1.ProtocolTCP, 6432)
	transition := fixtureVIPService("moving", "legacy", v1.ProtocolTCP, 6433)
	transition.Annotations[util.LogicalRouterAnnotation] = "target"
	unresolved := fixtureVIPService("unresolved", "", v1.ProtocolTCP, 6434)
	fixture, err := newFakeControllerWithOptions(t, &FakeControllerOptions{
		Services: []*v1.Service{current, transition, unresolved},
		Vpcs: []*fabricv1.Vpc{
			{ObjectMeta: metav1.ObjectMeta{Name: "legacy"}},
			{ObjectMeta: metav1.ObjectMeta{Name: "target"}, Status: fabricv1.VpcStatus{TCPLoadBalancer: "target-tcp"}},
		},
	})
	require.NoError(t, err)
	fixture.fakeController.config.EnableLb = true
	for _, name := range []string{"vpc-legacy-tcp-load", "target-tcp"} {
		fixture.mockOvnClient.EXPECT().GetLoadBalancer(name, true).Return(&ovnnb.LoadBalancer{Vips: map[string]string{
			"192.0.2.15:6432": "backend", "192.0.2.15:6433": "backend", "192.0.2.15:6434": "backend",
		}}, nil)
	}
	fixture.mockOvnClient.EXPECT().GetLoadBalancer(gomock.Any(), true).Return(nil, nil).AnyTimes()
	fixture.mockOvnClient.EXPECT().LoadBalancerDeleteVip("vpc-legacy-tcp-load", "192.0.2.15:6432", true).Return(nil)
	fixture.mockOvnClient.EXPECT().DeleteLoadBalancers(gomock.Any()).Return(nil)
	require.NoError(t, fixture.fakeController.gcLoadBalancer())
}
