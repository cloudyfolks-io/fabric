package controller

import (
	"fmt"
	"testing"

	nadv1 "github.com/k8snetworkplumbingwg/network-attachment-definition-client/pkg/apis/k8s.cni.cncf.io/v1"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	corev1 "k8s.io/api/core/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	fabricv1 "github.com/cloudyfolks-io/fabric/pkg/apis/fabric/v1"
	"github.com/cloudyfolks-io/fabric/pkg/util"
)

func TestFindServiceKey(t *testing.T) {
	tests := []struct {
		name          string
		endpointSlice *discoveryv1.EndpointSlice
		expectedKey   string
	}{
		{
			name: "valid endpoint slice with service name",
			endpointSlice: &discoveryv1.EndpointSlice{
				ObjectMeta: metav1.ObjectMeta{
					Namespace: "default",
					Labels: map[string]string{
						discoveryv1.LabelServiceName: "test-service",
					},
				},
			},
			expectedKey: "default/test-service",
		},
		{
			name: "endpoint slice with empty service name",
			endpointSlice: &discoveryv1.EndpointSlice{
				ObjectMeta: metav1.ObjectMeta{
					Namespace: "default",
					Labels: map[string]string{
						discoveryv1.LabelServiceName: "",
					},
				},
			},
			expectedKey: "",
		},
		{
			name: "endpoint slice with no labels",
			endpointSlice: &discoveryv1.EndpointSlice{
				ObjectMeta: metav1.ObjectMeta{
					Namespace: "default",
				},
			},
			expectedKey: "",
		},
		{
			name:          "nil endpoint slice",
			endpointSlice: nil,
			expectedKey:   "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := findServiceKey(tt.endpointSlice)
			if result != tt.expectedKey {
				t.Errorf("findServiceKey() = %q, want %q", result, tt.expectedKey)
			}
		})
	}
}

func TestEndpointReady(t *testing.T) {
	trueVal := true
	falseVal := false
	tests := []struct {
		name     string
		endpoint discoveryv1.Endpoint
		want     bool
	}{
		{
			name: "Ready is nil (should be ready)",
			endpoint: discoveryv1.Endpoint{
				Conditions: discoveryv1.EndpointConditions{
					Ready: nil,
				},
			},
			want: true,
		},
		{
			name: "Ready is true",
			endpoint: discoveryv1.Endpoint{
				Conditions: discoveryv1.EndpointConditions{
					Ready: &trueVal,
				},
			},
			want: true,
		},
		{
			name: "Ready is false",
			endpoint: discoveryv1.Endpoint{
				Conditions: discoveryv1.EndpointConditions{
					Ready: &falseVal,
				},
			},
			want: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := endpointReady(tt.endpoint)
			if got != tt.want {
				t.Errorf("endpointReady() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestGetEndpointTargetLSPNameFromProvider(t *testing.T) {
	tests := []struct {
		name     string
		pod      *corev1.Pod
		provider string
		expected string
	}{
		{
			name: "No provider, faulty pod",
			pod: &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "",
					Namespace: "",
				},
			},
			provider: "",
			expected: ".",
		},
		{
			name: "Endpoint empty with default provider and faulty pod",
			pod: &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "",
					Namespace: "",
				},
			},
			provider: util.OvnProvider,
			expected: ".",
		},
		{
			name: "Pod target with no provider",
			pod: &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "some-pod-1d8fn",
					Namespace: "default",
				},
			},
			provider: "",
			expected: "some-pod-1d8fn.default",
		},
		{
			name: "Pod target with default provider",
			pod: &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "some-pod-1d8fn",
					Namespace: "default",
				},
			},
			provider: util.OvnProvider,
			expected: "some-pod-1d8fn.default",
		},
		{
			name: "Pod target with custom provider",
			pod: &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "some-pod-1d8fn",
					Namespace: "default",
				},
			},
			provider: "custom.provider",
			expected: "some-pod-1d8fn.default.custom.provider",
		},
		{
			name: "VM target with no provider",
			pod: &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "virt-launcher-some-vm-67jd3",
					Namespace: "default",
					Annotations: map[string]string{
						fmt.Sprintf(util.VMAnnotationTemplate, util.OvnProvider): "some-vm",
					},
				},
			},
			provider: "",
			expected: "some-vm.default",
		},
		{
			name: "VM target with default provider",
			pod: &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "virt-launcher-some-vm-67jd3",
					Namespace: "default",
					Annotations: map[string]string{
						fmt.Sprintf(util.VMAnnotationTemplate, util.OvnProvider): "some-vm",
					},
				},
			},
			provider: util.OvnProvider,
			expected: "some-vm.default",
		},
		{
			name: "VM target with custom provider",
			pod: &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "virt-launcher-some-vm-67jd3",
					Namespace: "default",
					Annotations: map[string]string{
						fmt.Sprintf(util.VMAnnotationTemplate, "custom.provider"): "some-vm",
					},
				},
			},
			provider: "custom.provider",
			expected: "some-vm.default.custom.provider",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := getEndpointTargetLSPNameFromProvider(tt.pod, tt.provider)
			if result != tt.expected {
				t.Errorf("getEndpointTargetLSPName() = %q, want %q", result, tt.expected)
			}
		})
	}
}

func TestGetMatchingProviderForAddress(t *testing.T) {
	tests := []struct {
		name      string
		pod       *corev1.Pod
		providers []string
		address   string
		expected  string
	}{
		{
			name: "IP is on default provider and single stack",
			pod: &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Annotations: map[string]string{
						fmt.Sprintf(util.IPAddressAnnotationTemplate, util.OvnProvider): "1.1.1.1",
					},
				},
			},
			providers: []string{util.OvnProvider},
			address:   "1.1.1.1",
			expected:  util.OvnProvider,
		},
		{
			name: "IP is on default provider and dual stack",
			pod: &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Annotations: map[string]string{
						fmt.Sprintf(util.IPAddressAnnotationTemplate, util.OvnProvider): "1.1.1.1,fd00::a",
					},
				},
			},
			providers: []string{util.OvnProvider},
			address:   "fd00::a",
			expected:  util.OvnProvider,
		},
		{
			name: "IP is on custom provider and dual stack",
			pod: &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Annotations: map[string]string{
						fmt.Sprintf(util.IPAddressAnnotationTemplate, "custom.provider"): "1.1.1.1,fd00::a",
					},
				},
			},
			providers: []string{"custom.provider"},
			address:   "fd00::a",
			expected:  "custom.provider",
		},
		{
			name: "IP is on custom provider with multiple other providers present on the pod",
			pod: &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Annotations: map[string]string{
						fmt.Sprintf(util.IPAddressAnnotationTemplate, util.OvnProvider):  "10.0.0.1,fd10:0:0::1",
						fmt.Sprintf(util.IPAddressAnnotationTemplate, "first.provider"):  "1.1.1.1,fd00::a",
						fmt.Sprintf(util.IPAddressAnnotationTemplate, "second.provider"): "2.2.2.2,fd00::b",
						fmt.Sprintf(util.IPAddressAnnotationTemplate, "third.provider"):  "3.3.3.3,fd00::c",
					},
				},
			},
			providers: []string{util.OvnProvider, "first.provider", "second.provider", "third.provider"},
			address:   "fd00::b",
			expected:  "second.provider",
		},
		{
			name: "No provider is matching",
			pod: &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Annotations: map[string]string{
						fmt.Sprintf(util.IPAddressAnnotationTemplate, util.OvnProvider): "10.0.0.1,fd10:0:0::1",
					},
				},
			},
			providers: []string{util.OvnProvider},
			address:   "fd00::b",
			expected:  "",
		},
		{
			name: "No annotation is present",
			pod: &corev1.Pod{
				ObjectMeta: metav1.ObjectMeta{
					Annotations: nil,
				},
			},
			providers: []string{},
			address:   "fd00::b",
			expected:  "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := getMatchingProviderForAddress(tt.pod, tt.providers, tt.address)
			if result != tt.expected {
				t.Errorf("getMatchingProviderForAddress() = %q, want %q", result, tt.expected)
			}
		})
	}
}

func TestServiceHasSelector(t *testing.T) {
	tests := []struct {
		name     string
		service  *corev1.Service
		expected bool
	}{
		{
			name: "Service has no selector",
			service: &corev1.Service{Spec: corev1.ServiceSpec{
				Selector: nil,
			}},
			expected: false,
		},
		{
			name: "Service has empty selectors",
			service: &corev1.Service{Spec: corev1.ServiceSpec{
				Selector: map[string]string{},
			}},
			expected: false,
		},
		{
			name: "Service has one selector",
			service: &corev1.Service{Spec: corev1.ServiceSpec{
				Selector: map[string]string{
					"a": "b",
				},
			}},
			expected: true,
		},
		{
			name: "Service has multiple selectors",
			service: &corev1.Service{Spec: corev1.ServiceSpec{
				Selector: map[string]string{
					"a": "b",
					"c": "d",
				},
			}},
			expected: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := serviceHasSelector(tt.service)
			if result != tt.expected {
				t.Errorf("serviceHasSelector() = %t, want %t", result, tt.expected)
			}
		})
	}
}

func TestServiceHealthChecksDisabled(t *testing.T) {
	tests := []struct {
		name     string
		svc      *corev1.Service
		expected bool
	}{
		{
			name: "no annotation on the service",
			svc: &corev1.Service{
				ObjectMeta: metav1.ObjectMeta{
					Namespace: "default",
				},
			},
			expected: false,
		},
		{
			name: "unrelated annotation on the service",
			svc: &corev1.Service{
				ObjectMeta: metav1.ObjectMeta{
					Namespace:   "default",
					Annotations: map[string]string{"key": "value"},
				},
			},
			expected: false,
		},
		{
			name: "annotation to enable checks",
			svc: &corev1.Service{
				ObjectMeta: metav1.ObjectMeta{
					Namespace:   "default",
					Annotations: map[string]string{util.ServiceHealthCheck: "true"},
				},
			},
			expected: false,
		},
		{
			name: "malformed annotation to enable checks (will be ignored)",
			svc: &corev1.Service{
				ObjectMeta: metav1.ObjectMeta{
					Namespace:   "default",
					Annotations: map[string]string{util.ServiceHealthCheck: "invalid"},
				},
			},
			expected: false,
		},
		{
			name: "annotation to disable checks",
			svc: &corev1.Service{
				ObjectMeta: metav1.ObjectMeta{
					Namespace:   "default",
					Annotations: map[string]string{util.ServiceHealthCheck: "false"},
				},
			},
			expected: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := serviceHealthChecksDisabled(tt.svc)
			if result != tt.expected {
				t.Errorf("findServiceKey() = %t, want %t", result, tt.expected)
			}
		})
	}
}

func TestReplaceEndpointAddressesWithSecondaryIPs(t *testing.T) {
	tests := []struct {
		name               string
		endpointSlices     []*discoveryv1.EndpointSlice
		pods               []*corev1.Pod
		networkAttachments []*nadv1.NetworkAttachmentDefinition
		subnets            []*fabricv1.Subnet
		expectedChanges    map[string]string
		expectError        bool
		description        string
	}{
		{
			name: "Replace primary IP with secondary IP from network attachment",
			endpointSlices: []*discoveryv1.EndpointSlice{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "test-service-slice",
						Namespace: "default",
					},
					Endpoints: []discoveryv1.Endpoint{
						{
							Addresses: []string{"10.244.0.5"},
							TargetRef: &corev1.ObjectReference{
								Kind: util.KindPod,
								Name: "test-pod-1",
							},
						},
					},
				},
			},
			pods: []*corev1.Pod{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "test-pod-1",
						Namespace: "default",
						Annotations: map[string]string{
							nadv1.NetworkAttachmentAnnot: `[{"name": "net1"}]`,

							fmt.Sprintf(util.LogicalSwitchAnnotationTemplate, "net1.default.fabric"): "net1-subnet",
							fmt.Sprintf(util.LogicalRouterAnnotationTemplate, "net1.default.fabric"): "net1-vpc",
							fmt.Sprintf(util.IPAddressAnnotationTemplate, "net1.default.fabric"):     "192.168.1.10",
						},
					},
					Status: corev1.PodStatus{
						PodIP: "10.244.0.5",
					},
				},
			},
			networkAttachments: []*nadv1.NetworkAttachmentDefinition{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "net1",
						Namespace: "default",
					},
					Spec: nadv1.NetworkAttachmentDefinitionSpec{
						Config: `{
							"cniVersion": "0.3.1",
							"name": "net1",
							"type": "fabric",
							"server_socket": "/run/openvswitch/fabric-daemon.sock",
							"provider": "net1.default.fabric"
						}`,
					},
				},
			},
			subnets: []*fabricv1.Subnet{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name: "net1-subnet",
					},
					Spec: fabricv1.SubnetSpec{
						CIDRBlock: "192.168.1.0/24",
						Provider:  "net1.default.fabric",
					},
				},
				{
					ObjectMeta: metav1.ObjectMeta{
						Name: "ovn-default",
					},
					Spec: fabricv1.SubnetSpec{
						CIDRBlock: "10.244.0.0/24",
						Provider:  util.OvnProvider,
					},
				},
			},
			expectedChanges: map[string]string{
				"10.244.0.5": "192.168.1.10",
			},
			expectError: false,
			description: "Should replace primary IP with secondary IP from first provider",
		},
		{
			name: "Replace primary IP with secondary IP from network attachment with format <namespace>/<name>",
			endpointSlices: []*discoveryv1.EndpointSlice{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "test-service-slice",
						Namespace: "default",
					},
					Endpoints: []discoveryv1.Endpoint{
						{
							Addresses: []string{"10.244.0.5"},
							TargetRef: &corev1.ObjectReference{
								Kind: util.KindPod,
								Name: "test-pod-1",
							},
						},
					},
				},
			},
			pods: []*corev1.Pod{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "test-pod-1",
						Namespace: "default",
						Annotations: map[string]string{
							nadv1.NetworkAttachmentAnnot: "default/net1",

							fmt.Sprintf(util.LogicalSwitchAnnotationTemplate, "net1.default.fabric"): "net1-subnet",
							fmt.Sprintf(util.LogicalRouterAnnotationTemplate, "net1.default.fabric"): "net1-vpc",
							fmt.Sprintf(util.IPAddressAnnotationTemplate, "net1.default.fabric"):     "192.168.1.10",
						},
					},
					Status: corev1.PodStatus{
						PodIP: "10.244.0.5",
					},
				},
			},
			networkAttachments: []*nadv1.NetworkAttachmentDefinition{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "net1",
						Namespace: "default",
					},
					Spec: nadv1.NetworkAttachmentDefinitionSpec{
						Config: `{
							"cniVersion": "0.3.1",
							"name": "net1",
							"type": "fabric",
							"server_socket": "/run/openvswitch/fabric-daemon.sock",
							"provider": "net1.default.fabric"
						}`,
					},
				},
			},
			subnets: []*fabricv1.Subnet{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name: "net1-subnet",
					},
					Spec: fabricv1.SubnetSpec{
						CIDRBlock: "192.168.1.0/24",
						Provider:  "net1.default.fabric",
					},
				},
				{
					ObjectMeta: metav1.ObjectMeta{
						Name: "ovn-default",
					},
					Spec: fabricv1.SubnetSpec{
						CIDRBlock: "10.244.0.0/24",
						Provider:  util.OvnProvider,
					},
				},
			},
			expectedChanges: map[string]string{
				"10.244.0.5": "192.168.1.10",
			},
			expectError: false,
			description: "Should replace primary IP with secondary IP from first provider",
		},
		{
			name: "Pod without network attachment - no changes",
			endpointSlices: []*discoveryv1.EndpointSlice{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "test-service-slice",
						Namespace: "default",
					},
					Endpoints: []discoveryv1.Endpoint{
						{
							Addresses: []string{"10.244.0.5"},
							TargetRef: &corev1.ObjectReference{
								Kind: util.KindPod,
								Name: "test-pod-1",
							},
						},
					},
				},
			},
			pods: []*corev1.Pod{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "test-pod-1",
						Namespace: "default",

						Annotations: map[string]string{
							fmt.Sprintf(util.LogicalSwitchAnnotationTemplate, util.OvnProvider): "default-subnet",
							fmt.Sprintf(util.LogicalRouterAnnotationTemplate, util.OvnProvider): "ovn-cluster",
							fmt.Sprintf(util.IPAddressAnnotationTemplate, util.OvnProvider):     "10.244.0.5",
						},
					},
					Status: corev1.PodStatus{
						PodIP: "10.244.0.5",
					},
				},
			},
			networkAttachments: []*nadv1.NetworkAttachmentDefinition{},
			subnets: []*fabricv1.Subnet{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name: "default-subnet",
					},
					Spec: fabricv1.SubnetSpec{
						CIDRBlock: "10.244.0.0/24",
						Provider:  util.OvnProvider,
					},
				},
			},
			expectedChanges: map[string]string{},
			expectError:     false,
			description:     "Should not change endpoints for pods without network attachments",
		},
		{
			name: "Pod with multiple network attachments",
			endpointSlices: []*discoveryv1.EndpointSlice{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "test-service-slice",
						Namespace: "default",
					},
					Endpoints: []discoveryv1.Endpoint{
						{
							Addresses: []string{"10.244.0.5"},
							TargetRef: &corev1.ObjectReference{
								Kind: util.KindPod,
								Name: "test-pod-1",
							},
						},
					},
				},
			},
			pods: []*corev1.Pod{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "test-pod-1",
						Namespace: "default",
						Annotations: map[string]string{
							nadv1.NetworkAttachmentAnnot: `[{"name": "net1"}, {"name": "net2"}]`,

							fmt.Sprintf(util.LogicalSwitchAnnotationTemplate, "net1.default.fabric"): "net1-subnet",
							fmt.Sprintf(util.LogicalRouterAnnotationTemplate, "net1.default.fabric"): "net1-vpc",
							fmt.Sprintf(util.IPAddressAnnotationTemplate, "net1.default.fabric"):     "192.168.1.10",

							fmt.Sprintf(util.LogicalSwitchAnnotationTemplate, "net2.default.fabric"): "net2-subnet",
							fmt.Sprintf(util.LogicalRouterAnnotationTemplate, "net2.default.fabric"): "net2-vpc",
							fmt.Sprintf(util.IPAddressAnnotationTemplate, "net2.default.fabric"):     "192.168.2.10",
						},
					},
					Status: corev1.PodStatus{
						PodIP: "10.244.0.5",
					},
				},
			},
			networkAttachments: []*nadv1.NetworkAttachmentDefinition{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "net1",
						Namespace: "default",
					},
					Spec: nadv1.NetworkAttachmentDefinitionSpec{
						Config: `{
							"cniVersion": "0.3.1",
							"name": "net1",
							"type": "fabric",
							"server_socket": "/run/openvswitch/fabric-daemon.sock",
							"provider": "net1.default.fabric"
						}`,
					},
				},
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "net2",
						Namespace: "default",
					},
					Spec: nadv1.NetworkAttachmentDefinitionSpec{
						Config: `{
							"cniVersion": "0.3.1",
							"name": "net2",
							"type": "fabric",
							"server_socket": "/run/openvswitch/fabric-daemon.sock",
							"provider": "net2.default.fabric"
						}`,
					},
				},
			},
			subnets: []*fabricv1.Subnet{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name: "net1-subnet",
					},
					Spec: fabricv1.SubnetSpec{
						CIDRBlock: "192.168.1.0/24",
						Provider:  "net1.default.fabric",
					},
				},
				{
					ObjectMeta: metav1.ObjectMeta{
						Name: "net2-subnet",
					},
					Spec: fabricv1.SubnetSpec{
						CIDRBlock: "192.168.2.0/24",
						Provider:  "net2.default.fabric",
					},
				},
				{
					ObjectMeta: metav1.ObjectMeta{
						Name: "ovn-default",
					},
					Spec: fabricv1.SubnetSpec{
						CIDRBlock: "10.244.0.0/24",
						Provider:  util.OvnProvider,
					},
				},
			},

			expectedChanges: map[string]string{
				"10.244.0.5": "192.168.1.10",
			},
			expectError: false,
			description: "Should replace primary IP with secondary IP from first provider",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			fakeController, err := newFakeControllerWithOptions(t, &FakeControllerOptions{
				NetworkAttachments: tt.networkAttachments,
				Subnets:            tt.subnets,
				Pods:               tt.pods,
			})
			require.NoError(t, err, "Failed to create fake controller")

			controller := fakeController.fakeController

			originalAddresses := make(map[string][]string)
			for i, slice := range tt.endpointSlices {
				for j, endpoint := range slice.Endpoints {
					key := fmt.Sprintf("%d-%d", i, j)
					originalAddresses[key] = make([]string, len(endpoint.Addresses))
					copy(originalAddresses[key], endpoint.Addresses)
				}
			}

			err = controller.replaceEndpointAddressesWithSecondaryIPs(tt.endpointSlices, tt.pods)

			if tt.expectError {
				assert.Error(t, err, "Expected an error but got none")
				return
			}
			require.NoError(t, err, "Unexpected error from replaceEndpointAddressesWithSecondaryIPs")

			changesFound := make(map[string]string)
			for i, slice := range tt.endpointSlices {
				for j, endpoint := range slice.Endpoints {
					key := fmt.Sprintf("%d-%d", i, j)
					originalAddrs := originalAddresses[key]

					for k, newAddr := range endpoint.Addresses {
						if k < len(originalAddrs) && originalAddrs[k] != newAddr {
							changesFound[originalAddrs[k]] = newAddr
						}
					}
				}
			}

			assert.Equal(t, len(tt.expectedChanges), len(changesFound),
				"Number of changes mismatch. Expected: %v, Got: %v", tt.expectedChanges, changesFound)

			for originalIP, expectedNewIP := range tt.expectedChanges {
				actualNewIP, exists := changesFound[originalIP]
				assert.True(t, exists, "Expected change from %s to %s, but no change found", originalIP, expectedNewIP)
				if exists {
					assert.Equal(t, expectedNewIP, actualNewIP, "Expected change from %s to %s, but got %s", originalIP, expectedNewIP, actualNewIP)
				}
			}
		})
	}
}

const twoInterfaceProvider = "vpc.kube-system." + util.OvnProvider

func twoInterfaceTestOptions(pod *corev1.Pod) *FakeControllerOptions {
	return &FakeControllerOptions{
		Pods: []*corev1.Pod{pod},
		NetworkAttachments: []*nadv1.NetworkAttachmentDefinition{{
			ObjectMeta: metav1.ObjectMeta{Name: "vpc", Namespace: "kube-system"},
			Spec: nadv1.NetworkAttachmentDefinitionSpec{
				Config: `{"cniVersion": "0.3.1", "name": "vpc", "type": "fabric", "provider": "` + twoInterfaceProvider + `"}`,
			},
		}},
		Subnets: []*fabricv1.Subnet{
			{
				ObjectMeta: metav1.ObjectMeta{Name: util.DefaultSubnet},
				Spec:       fabricv1.SubnetSpec{CIDRBlock: "10.244.0.0/24", Vpc: util.DefaultVpc, Provider: util.OvnProvider},
			},
			{
				ObjectMeta: metav1.ObjectMeta{Name: "customer"},
				Spec:       fabricv1.SubnetSpec{CIDRBlock: "192.168.1.0/24", Vpc: "tenant-vpc", Provider: util.OvnProvider},
			},
		},
	}
}

func twoInterfacePod(secondaryIPs string) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "backend",
			Namespace: "default",
			Annotations: map[string]string{
				nadv1.NetworkAttachmentAnnot: "kube-system/vpc",

				fmt.Sprintf(util.LogicalSwitchAnnotationTemplate, util.OvnProvider): util.DefaultSubnet,
				fmt.Sprintf(util.LogicalRouterAnnotationTemplate, util.OvnProvider): util.DefaultVpc,
				fmt.Sprintf(util.IPAddressAnnotationTemplate, util.OvnProvider):     "10.244.0.5",

				fmt.Sprintf(util.LogicalSwitchAnnotationTemplate, twoInterfaceProvider): "customer",
				fmt.Sprintf(util.LogicalRouterAnnotationTemplate, twoInterfaceProvider): "tenant-vpc",
				fmt.Sprintf(util.IPAddressAnnotationTemplate, twoInterfaceProvider):     secondaryIPs,
			},
		},
		Status: corev1.PodStatus{PodIP: "10.244.0.5"},
	}
}

func twoInterfaceService(annotations map[string]string, selector map[string]string) *corev1.Service {
	return &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Name: "svc", Namespace: "default", Annotations: annotations},
		Spec:       corev1.ServiceSpec{Selector: selector},
	}
}

func twoInterfaceSlice(addressType discoveryv1.AddressType, address string) *discoveryv1.EndpointSlice {
	return &discoveryv1.EndpointSlice{
		ObjectMeta:  metav1.ObjectMeta{Name: "svc-slice-" + string(addressType), Namespace: "default"},
		AddressType: addressType,
		Ports:       []discoveryv1.EndpointPort{{Name: new("http"), Port: new(int32(80))}},
		Endpoints: []discoveryv1.Endpoint{{
			Addresses: []string{address},
			TargetRef: &corev1.ObjectReference{Kind: util.KindPod, Namespace: "default", Name: "backend"},
		}},
	}
}

func TestReplaceEndpointAddressesWithVpcIPs(t *testing.T) {
	selector := map[string]string{"app": "db"}
	tests := []struct {
		name        string
		annotations map[string]string
		selector    map[string]string
		secondary   string
		want        string
	}{
		{
			name:        "vpc of the secondary interface",
			annotations: map[string]string{util.LogicalRouterAnnotation: "tenant-vpc"},
			selector:    selector,
			secondary:   "192.168.1.10",
			want:        "192.168.1.10",
		},
		{
			name:        "vpc and subnet of the secondary interface",
			annotations: map[string]string{util.LogicalRouterAnnotation: "tenant-vpc", util.LogicalSwitchAnnotation: "customer"},
			selector:    selector,
			secondary:   "192.168.1.10",
			want:        "192.168.1.10",
		},
		{
			name:        "subnet only",
			annotations: map[string]string{util.LogicalSwitchAnnotation: "customer"},
			selector:    selector,
			secondary:   "192.168.1.10",
			want:        "192.168.1.10",
		},
		{
			name:        "vpc of the primary interface keeps the primary address",
			annotations: map[string]string{util.LogicalRouterAnnotation: util.DefaultVpc},
			selector:    selector,
			secondary:   "192.168.1.10",
			want:        "10.244.0.5",
		},
		{
			name:        "subnet of the primary interface keeps the primary address",
			annotations: map[string]string{util.LogicalSwitchAnnotation: util.DefaultSubnet},
			selector:    selector,
			secondary:   "192.168.1.10",
			want:        "10.244.0.5",
		},
		{
			name:        "vpc matches but subnet does not",
			annotations: map[string]string{util.LogicalRouterAnnotation: "tenant-vpc", util.LogicalSwitchAnnotation: "other"},
			selector:    selector,
			secondary:   "192.168.1.10",
			want:        "10.244.0.5",
		},
		{
			name:        "unknown vpc",
			annotations: map[string]string{util.LogicalRouterAnnotation: "other-vpc"},
			selector:    selector,
			secondary:   "192.168.1.10",
			want:        "10.244.0.5",
		},
		{
			name:      "service without vpc or subnet",
			selector:  selector,
			secondary: "192.168.1.10",
			want:      "10.244.0.5",
		},
		{
			name:        "service without selector",
			annotations: map[string]string{util.LogicalRouterAnnotation: "tenant-vpc"},
			secondary:   "192.168.1.10",
			want:        "10.244.0.5",
		},
		{
			name:        "secondary interface without address",
			annotations: map[string]string{util.LogicalRouterAnnotation: "tenant-vpc"},
			selector:    selector,
			secondary:   "",
			want:        "10.244.0.5",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			fc, err := newFakeControllerWithOptions(t, twoInterfaceTestOptions(twoInterfacePod(tt.secondary)))
			require.NoError(t, err)

			original := twoInterfaceSlice(discoveryv1.AddressTypeIPv4, "10.244.0.5")
			slices := []*discoveryv1.EndpointSlice{original}

			err = fc.fakeController.replaceEndpointAddressesWithVpcIPs(twoInterfaceService(tt.annotations, tt.selector), slices)
			require.NoError(t, err)

			require.Len(t, slices, 1)
			require.Len(t, slices[0].Endpoints, 1)
			assert.Equal(t, []string{tt.want}, slices[0].Endpoints[0].Addresses)
			assert.Equal(t, []string{"10.244.0.5"}, original.Endpoints[0].Addresses, "the shared slice must not be mutated")
		})
	}
}

func TestReplaceEndpointAddressesWithVpcIPsDualStack(t *testing.T) {
	fc, err := newFakeControllerWithOptions(t, twoInterfaceTestOptions(twoInterfacePod("192.168.1.10,fd00:1::10")))
	require.NoError(t, err)

	slices := []*discoveryv1.EndpointSlice{
		twoInterfaceSlice(discoveryv1.AddressTypeIPv4, "10.244.0.5"),
		twoInterfaceSlice(discoveryv1.AddressTypeIPv6, "fd00:244::5"),
	}
	svc := twoInterfaceService(map[string]string{util.LogicalRouterAnnotation: "tenant-vpc"}, map[string]string{"app": "db"})

	require.NoError(t, fc.fakeController.replaceEndpointAddressesWithVpcIPs(svc, slices))
	assert.Equal(t, []string{"192.168.1.10"}, slices[0].Endpoints[0].Addresses)
	assert.Equal(t, []string{"fd00:1::10"}, slices[1].Endpoints[0].Addresses)
}

func TestReplaceEndpointAddressesWithVpcIPsDropsFamilyWithoutSecondaryAddress(t *testing.T) {
	fc, err := newFakeControllerWithOptions(t, twoInterfaceTestOptions(twoInterfacePod("192.168.1.10")))
	require.NoError(t, err)

	slices := []*discoveryv1.EndpointSlice{twoInterfaceSlice(discoveryv1.AddressTypeIPv6, "fd00:244::5")}
	svc := twoInterfaceService(map[string]string{util.LogicalRouterAnnotation: "tenant-vpc"}, map[string]string{"app": "db"})

	require.NoError(t, fc.fakeController.replaceEndpointAddressesWithVpcIPs(svc, slices))
	assert.Empty(t, slices[0].Endpoints[0].Addresses)
}

func TestReplaceEndpointAddressesWithVpcIPsSkipsMissingPod(t *testing.T) {
	fc, err := newFakeControllerWithOptions(t, twoInterfaceTestOptions(twoInterfacePod("192.168.1.10")))
	require.NoError(t, err)

	slice := twoInterfaceSlice(discoveryv1.AddressTypeIPv4, "10.244.0.9")
	slice.Endpoints[0].TargetRef.Name = "gone"
	slices := []*discoveryv1.EndpointSlice{slice}
	svc := twoInterfaceService(map[string]string{util.LogicalRouterAnnotation: "tenant-vpc"}, map[string]string{"app": "db"})

	require.NoError(t, fc.fakeController.replaceEndpointAddressesWithVpcIPs(svc, slices))
	assert.Equal(t, []string{"10.244.0.9"}, slices[0].Endpoints[0].Addresses)
}

func TestSecondaryAddressMovesLoadBalancerToTheSecondaryVpc(t *testing.T) {
	fc, err := newFakeControllerWithOptions(t, twoInterfaceTestOptions(twoInterfacePod("192.168.1.10")))
	require.NoError(t, err)
	ctrl := fc.fakeController

	slices := []*discoveryv1.EndpointSlice{twoInterfaceSlice(discoveryv1.AddressTypeIPv4, "10.244.0.5")}
	svc := twoInterfaceService(map[string]string{util.LogicalRouterAnnotation: "tenant-vpc"}, map[string]string{"app": "db"})

	vpcName, subnetName, err := ctrl.getVpcAndSubnetForEndpoints(slices, svc)
	require.NoError(t, err)
	assert.Equal(t, util.DefaultVpc, vpcName, "the primary address sits in the default vpc")
	assert.Equal(t, util.DefaultSubnet, subnetName)

	require.NoError(t, ctrl.replaceEndpointAddressesWithVpcIPs(svc, slices))
	vpcName, subnetName, err = ctrl.getVpcAndSubnetForEndpoints(slices, svc)
	require.NoError(t, err)
	assert.Equal(t, "tenant-vpc", vpcName)
	assert.Equal(t, "customer", subnetName)

	backends := ctrl.getEndpointBackend(slices, corev1.ServicePort{Name: "http"}, "192.168.1.100")
	assert.Equal(t, []string{"192.168.1.10:80"}, backends)
}
