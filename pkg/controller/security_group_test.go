package controller

import (
	"encoding/json"
	"errors"
	"testing"

	"github.com/stretchr/testify/require"
	"go.uber.org/mock/gomock"
	k8serrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	k8stesting "k8s.io/client-go/testing"

	fabricv1 "github.com/cloudyfolks-io/fabric/pkg/apis/fabric/v1"
	fabricfake "github.com/cloudyfolks-io/fabric/pkg/client/clientset/versioned/fake"
	"github.com/cloudyfolks-io/fabric/pkg/ovs"
	"github.com/cloudyfolks-io/fabric/pkg/ovsdb/ovnnb"
	"github.com/cloudyfolks-io/fabric/pkg/util"
)

func mockLsp() *ovnnb.LogicalSwitchPort {
	return &ovnnb.LogicalSwitchPort{
		ExternalIDs: map[string]string{
			"associated_sg_default-securitygroup": "false",
			"associated_sg_sg":                    "true",
			"security_groups":                     "sg",
		},
	}
}

func TestSecurityGroupAcknowledgesBothDirectionsForTheCurrentGeneration(t *testing.T) {
	fake := newFakeController(t)
	sg := &fabricv1.SecurityGroup{
		ObjectMeta: metav1.ObjectMeta{Name: "receipt-sg", Generation: 3},
		Status: fabricv1.SecurityGroupStatus{
			ObservedGeneration: 2, IngressLastSyncSuccess: true, EgressLastSyncSuccess: true,
		},
	}
	fail := errors.New("base egress ACL failed")
	for _, baseResult := range []error{fail, nil} {
		gomock.InOrder(
			fake.mockOvnClient.EXPECT().UpdateSgACL(sg, ovnnb.ACLDirectionToLport).Return(nil),
			fake.mockOvnClient.EXPECT().CreateSgBaseACL(sg.Name, ovnnb.ACLDirectionToLport).Return(nil),
			fake.mockOvnClient.EXPECT().UpdateSgACL(sg, ovnnb.ACLDirectionFromLport).Return(nil),
			fake.mockOvnClient.EXPECT().CreateSgBaseACL(sg.Name, ovnnb.ACLDirectionFromLport).Return(baseResult),
		)
		if baseResult != nil {
			require.ErrorIs(t, fake.fakeController.syncSgACLs(sg, false), fail)
			require.EqualValues(t, 2, sg.Status.ObservedGeneration)
			require.False(t, sg.Status.EgressLastSyncSuccess)
			continue
		}
		require.NoError(t, fake.fakeController.syncSgACLs(sg, false))
		require.Equal(t, sg.Generation, sg.Status.ObservedGeneration)
		require.True(t, sg.Status.IngressLastSyncSuccess)
		require.True(t, sg.Status.EgressLastSyncSuccess)
		require.NotEmpty(t, sg.Status.IngressMd5)
		require.NotEmpty(t, sg.Status.EgressMd5)
	}
	status := sg.Status
	require.NoError(t, fake.fakeController.syncSgACLs(sg, false))
	require.Equal(t, status, sg.Status)
}

func TestSecurityGroupReceiptConflictRequiresReconciliation(t *testing.T) {
	sg := &fabricv1.SecurityGroup{
		ObjectMeta: metav1.ObjectMeta{Name: "receipt-sg", ResourceVersion: "17", Generation: 5},
		Status:     fabricv1.SecurityGroupStatus{ObservedGeneration: 5},
	}
	fabricClient := fabricfake.NewSimpleClientset()
	fabricClient.PrependReactor("patch", "security-groups", func(a k8stesting.Action) (bool, runtime.Object, error) {
		action := a.(k8stesting.PatchAction)
		require.Equal(t, "status", action.GetSubresource())
		require.Equal(t, types.MergePatchType, action.GetPatchType())
		var payload fabricv1.SecurityGroup
		require.NoError(t, json.Unmarshal(action.GetPatch(), &payload))
		require.Equal(t, sg.ResourceVersion, payload.ResourceVersion)
		require.Equal(t, sg.Generation, payload.Status.ObservedGeneration)
		return true, nil, k8serrors.NewConflict(a.GetResource().GroupResource(), sg.Name, errors.New("the spec changed"))
	})
	c := &Controller{config: &Configuration{FabricClient: fabricClient}}
	require.True(t, k8serrors.IsConflict(c.patchSgStatus(sg)))
	require.Len(t, fabricClient.Actions(), 1)
}

func Test_getPortSg(t *testing.T) {
	t.Run("only have one sg", func(t *testing.T) {
		c := &Controller{}
		port := mockLsp()
		out, err := c.getPortSg(port)
		require.NoError(t, err)
		require.ElementsMatch(t, []string{"sg"}, out)
	})

	t.Run("have two and more sgs", func(t *testing.T) {
		c := &Controller{}
		port := mockLsp()
		port.ExternalIDs["associated_sg_default-securitygroup"] = "true"
		out, err := c.getPortSg(port)
		require.NoError(t, err)
		require.ElementsMatch(t, []string{"sg", "default-securitygroup"}, out)
	})
}

func Test_securityGroupALLNotExist(t *testing.T) {
	t.Parallel()

	fakeController := newFakeController(t)
	ctrl := fakeController.fakeController
	mockOvnClient := fakeController.mockOvnClient

	sgName := "sg"
	pgName := ovs.GetSgPortGroupName(sgName)

	t.Run("should return false when some port group exists", func(t *testing.T) {
		mockOvnClient.EXPECT().PortGroupExists(gomock.Eq(pgName)).Return(true, nil)
		mockOvnClient.EXPECT().PortGroupExists(gomock.Not(pgName)).Return(false, nil).Times(3)

		exist, err := ctrl.securityGroupAllNotExist([]string{sgName, "sg1", "sg2", "sg3"})
		require.NoError(t, err)
		require.False(t, exist)
	})

	t.Run("should return true when all port group don't exist", func(t *testing.T) {
		mockOvnClient.EXPECT().PortGroupExists(gomock.Any()).Return(false, nil).Times(3)

		exist, err := ctrl.securityGroupAllNotExist([]string{"sg1", "sg2", "sg3"})
		require.NoError(t, err)
		require.True(t, exist)
	})

	t.Run("should return true when sgs is empty", func(t *testing.T) {
		exist, err := ctrl.securityGroupAllNotExist([]string{})
		require.NoError(t, err)
		require.True(t, exist)
	})
}

func Test_validateSgRule(t *testing.T) {
	t.Parallel()

	ctrl := &Controller{}
	baseSG := func(rules ...fabricv1.SecurityGroupRule) *fabricv1.SecurityGroup {
		return &fabricv1.SecurityGroup{
			ObjectMeta: metav1.ObjectMeta{Name: "test-sg"},
			Spec: fabricv1.SecurityGroupSpec{
				Tier:         util.SecurityGroupAPITierMinimum,
				IngressRules: rules,
			},
		}
	}

	t.Run("valid local address as CIDR", func(t *testing.T) {
		t.Parallel()

		sg := baseSG(fabricv1.SecurityGroupRule{
			IPVersion:     "ipv4",
			Priority:      1,
			RemoteType:    fabricv1.SgRemoteTypeAddress,
			RemoteAddress: "10.0.0.0/8",
			LocalAddress:  "192.168.1.0/24",
			Protocol:      "all",
			Policy:        fabricv1.SgPolicy(ovnnb.ACLActionAllow),
		})
		err := ctrl.validateSgRule(sg)
		require.NoError(t, err)
	})

	t.Run("valid local address as IP", func(t *testing.T) {
		t.Parallel()

		sg := baseSG(fabricv1.SecurityGroupRule{
			IPVersion:     "ipv4",
			Priority:      1,
			RemoteType:    fabricv1.SgRemoteTypeAddress,
			RemoteAddress: "10.0.0.1",
			LocalAddress:  "192.168.1.100",
			Protocol:      "all",
			Policy:        fabricv1.SgPolicy(ovnnb.ACLActionAllow),
		})
		err := ctrl.validateSgRule(sg)
		require.NoError(t, err)
	})

	t.Run("invalid local address CIDR", func(t *testing.T) {
		t.Parallel()

		sg := baseSG(fabricv1.SecurityGroupRule{
			IPVersion:     "ipv4",
			Priority:      1,
			RemoteType:    fabricv1.SgRemoteTypeAddress,
			RemoteAddress: "10.0.0.1",
			LocalAddress:  "999.999.999.0/24",
			Protocol:      "all",
			Policy:        fabricv1.SgPolicy(ovnnb.ACLActionAllow),
		})
		err := ctrl.validateSgRule(sg)
		require.ErrorContains(t, err, "invalid CIDR")
	})

	t.Run("invalid local address IP", func(t *testing.T) {
		t.Parallel()

		sg := baseSG(fabricv1.SecurityGroupRule{
			IPVersion:     "ipv4",
			Priority:      1,
			RemoteType:    fabricv1.SgRemoteTypeAddress,
			RemoteAddress: "10.0.0.1",
			LocalAddress:  "not-an-ip",
			Protocol:      "all",
			Policy:        fabricv1.SgPolicy(ovnnb.ACLActionAllow),
		})
		err := ctrl.validateSgRule(sg)
		require.ErrorContains(t, err, "invalid ip address")
	})

	t.Run("valid local port range with TCP and local address", func(t *testing.T) {
		t.Parallel()

		sg := baseSG(fabricv1.SecurityGroupRule{
			IPVersion:          "ipv4",
			Priority:           1,
			RemoteType:         fabricv1.SgRemoteTypeAddress,
			RemoteAddress:      "10.0.0.1",
			LocalAddress:       "192.168.1.100",
			Protocol:           "tcp",
			Policy:             fabricv1.SgPolicy(ovnnb.ACLActionAllow),
			PortRangeMin:       80,
			PortRangeMax:       443,
			SourcePortRangeMin: 1024,
			SourcePortRangeMax: 65535,
		})
		err := ctrl.validateSgRule(sg)
		require.NoError(t, err)
	})

	t.Run("invalid local port range out of bounds", func(t *testing.T) {
		t.Parallel()

		sg := baseSG(fabricv1.SecurityGroupRule{
			IPVersion:          "ipv4",
			Priority:           1,
			RemoteType:         fabricv1.SgRemoteTypeAddress,
			RemoteAddress:      "10.0.0.1",
			LocalAddress:       "192.168.1.100",
			Protocol:           "tcp",
			Policy:             fabricv1.SgPolicy(ovnnb.ACLActionAllow),
			PortRangeMin:       80,
			PortRangeMax:       443,
			SourcePortRangeMin: 0,
			SourcePortRangeMax: 65535,
		})
		err := ctrl.validateSgRule(sg)
		require.ErrorContains(t, err, "sourcePortRange is out of range")
	})

	t.Run("invalid local port range min greater than max", func(t *testing.T) {
		t.Parallel()

		sg := baseSG(fabricv1.SecurityGroupRule{
			IPVersion:          "ipv4",
			Priority:           1,
			RemoteType:         fabricv1.SgRemoteTypeAddress,
			RemoteAddress:      "10.0.0.1",
			LocalAddress:       "192.168.1.100",
			Protocol:           "udp",
			Policy:             fabricv1.SgPolicy(ovnnb.ACLActionAllow),
			PortRangeMin:       80,
			PortRangeMax:       443,
			SourcePortRangeMin: 9000,
			SourcePortRangeMax: 8000,
		})
		err := ctrl.validateSgRule(sg)
		require.ErrorContains(t, err, "range Minimum value greater than maximum value")
	})
}
