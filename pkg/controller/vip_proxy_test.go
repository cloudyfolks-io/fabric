package controller

import (
	"errors"
	"testing"

	"github.com/stretchr/testify/require"
	"go.uber.org/mock/gomock"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/cache"

	fabricv1 "github.com/cloudyfolks-io/fabric/pkg/apis/fabric/v1"
	fabriclisters "github.com/cloudyfolks-io/fabric/pkg/client/listers/fabric/v1"
	"github.com/cloudyfolks-io/fabric/pkg/ovsdb/ovnnb"
	"github.com/cloudyfolks-io/fabric/pkg/util"
)

func TestSwitchLBVipProxyIPs(t *testing.T) {
	newVIP := func(name, subnet, kind, v4, v6 string) *fabricv1.Vip {
		return &fabricv1.Vip{
			ObjectMeta: metav1.ObjectMeta{Name: name},
			Spec:       fabricv1.VipSpec{Subnet: subnet, Type: kind},
			Status:     fabricv1.VipStatus{V4ip: v4, V6ip: v6},
		}
	}
	active := newVIP("first", "tenant", util.SwitchLBRuleVip, "10.0.0.10", "fd00::10")
	second := newVIP("second", "tenant", util.SwitchLBRuleVip, "10.0.0.11", "")
	deleting := newVIP("deleted", "tenant", util.SwitchLBRuleVip, "10.0.0.12", "")
	now := metav1.Now()
	deleting.DeletionTimestamp = &now
	otherSubnet := newVIP("other", "another-vpc", util.SwitchLBRuleVip, "10.0.0.13", "")
	otherType := newVIP("virtual", "tenant", "", "10.0.0.14", "")
	vips := []*fabricv1.Vip{active, second, deleting, otherSubnet, otherType}
	require.Equal(t, []string{"10.0.0.10", "10.0.0.11", "fd00::10"},
		switchLBVipProxyIPs(vips, "tenant", "", "10.0.0.10"))
	require.Equal(t, []string{"10.0.0.10", "10.0.0.11", "10.0.0.15", "fd00::10"},
		switchLBVipProxyIPs(vips, "tenant", "10.0.0.15"))
	require.Empty(t, switchLBVipProxyIPs(vips, "missing"))
}

func TestVipIPAMAllocationPreservesBothFamiliesOnRestart(t *testing.T) {
	for _, tc := range []struct {
		kind, v4, v6, wantIP, wantMAC string
	}{
		{"", "10.0.0.10", "", "10.0.0.10", "aa:bb:cc:dd:ee:ff"},
		{util.SwitchLBRuleVip, "10.0.0.10", "", "10.0.0.10", ""},
		{util.SwitchLBRuleVip, "", "fd00::10", "fd00::10", ""},
		{util.SwitchLBRuleVip, "10.0.0.10", "fd00::10", "10.0.0.10,fd00::10", ""},
	} {
		vip := &fabricv1.Vip{
			Spec:   fabricv1.VipSpec{Type: tc.kind},
			Status: fabricv1.VipStatus{V4ip: tc.v4, V6ip: tc.v6, Mac: "aa:bb:cc:dd:ee:ff"},
		}
		ip, mac := vipIPAMAllocation(vip)
		require.Equal(t, tc.wantIP, ip)
		require.Equal(t, tc.wantMAC, mac)
	}
}

func TestSwitchLBVipProxyPrecedesStalePortRemoval(t *testing.T) {
	for _, proxyFails := range []bool{false, true} {
		t.Run(map[bool]string{false: "success", true: "proxy failure preserves stale responder"}[proxyFails], func(t *testing.T) {
			subnet := &fabricv1.Subnet{
				ObjectMeta: metav1.ObjectMeta{Name: "tenant"},
				Spec:       fabricv1.SubnetSpec{Vpc: "vpc", Provider: util.OvnProvider},
			}
			vpc := &fabricv1.Vpc{
				ObjectMeta: metav1.ObjectMeta{Name: "vpc"},
				Status:     fabricv1.VpcStatus{Router: "vpc"},
			}
			fc, err := newFakeControllerWithOptions(t, &FakeControllerOptions{
				Subnets: []*fabricv1.Subnet{subnet}, Vpcs: []*fabricv1.Vpc{vpc},
			})
			require.NoError(t, err)
			vip := &fabricv1.Vip{
				ObjectMeta: metav1.ObjectMeta{Name: "address"},
				Spec:       fabricv1.VipSpec{Subnet: "tenant", Namespace: "ns", Type: util.SwitchLBRuleVip, V4ip: "10.0.0.10"},
			}
			other := vip.DeepCopy()
			other.Name = "other"
			other.Status.V4ip = "10.0.0.11"
			indexer := cache.NewIndexer(cache.MetaNamespaceKeyFunc, cache.Indexers{})
			require.NoError(t, indexer.Add(other))
			fc.fakeController.virtualIpsLister = fabriclisters.NewVipLister(indexer)
			fc.mockOvnClient.EXPECT().GetLogicalRouterPort("vpc-tenant", false).Return(
				&ovnnb.LogicalRouterPort{MAC: "02:00:00:00:00:01"}, nil,
			)
			proxy := fc.mockOvnClient.EXPECT().SetLogicalSwitchPortArpProxyIPs("tenant-vpc", "02:00:00:00:00:01", []string{"10.0.0.10", "10.0.0.11"})
			if proxyFails {
				proxy.Return(errors.New("database unavailable"))
				require.Error(t, fc.fakeController.reconcileSwitchLBVipPort(vip))
			} else {
				gomock.InOrder(proxy.Return(nil),
					fc.mockOvnClient.EXPECT().DeleteLogicalSwitchPort("address.ns").Return(nil))
				require.NoError(t, fc.fakeController.reconcileSwitchLBVipPort(vip))
			}
		})
	}
}
