package switch_lb_rule

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/onsi/ginkgo/v2"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/util/intstr"

	fabricv1 "github.com/cloudyfolks-io/fabric/pkg/apis/fabric/v1"
	"github.com/cloudyfolks-io/fabric/pkg/util"
	"github.com/cloudyfolks-io/fabric/test/e2e/framework"
)

const (
	moveVIPPort = int32(6432)
	movePodPort = int32(8080)
)

type movePath struct {
	vpc     string
	subnet  string
	client  string
	backend *corev1.Pod
}

type moveProbe struct {
	status      int
	connections int
	backend     string
}

var _ = framework.Describe("[group:slr-vpc-move]", func() {
	if os.Getenv("E2E_FABRIC_VPC_MOVE") != "1" {
		return
	}
	f := framework.NewDefaultFramework("slr-vpc-move")

	ginkgo.It("moves a live SwitchLBRule between VPCs at its original socket", ginkgo.Label("FabricVPCMoveLab"), func() {
		if !f.IsIPv4() {
			ginkgo.Skip("This lab requires a single-stack IPv4 cluster")
		}
		suffix := framework.RandomSuffix()
		source := createMovePath(f, "source-"+suffix)
		target := createMovePath(f, "target-"+suffix)
		control := createMovePath(f, "control-"+suffix)
		serviceClient := f.ServiceClient()
		slrClient := f.SwitchLBRuleClient()

		ginkgo.By("Creating a live control frontend that owns the same socket in another VPC")
		controlService := framework.MakeService("control-"+suffix, corev1.ServiceTypeClusterIP,
			movePathAnnotations(control), map[string]string{"app": control.backend.Name},
			[]corev1.ServicePort{{Name: "http", Protocol: corev1.ProtocolTCP, Port: moveVIPPort, TargetPort: intstr.FromInt32(movePodPort)}},
			corev1.ServiceAffinityNone)
		controlService = serviceClient.CreateSync(controlService, func(svc *corev1.Service) (bool, error) {
			return svc.Spec.ClusterIP != "", nil
		}, "the control Service has a ClusterIP")
		ginkgo.DeferCleanup(serviceClient.DeleteSync, controlService.Name)
		vip := controlService.Spec.ClusterIP
		socket := util.JoinHostPort(vip, moveVIPPort)

		ginkgo.By("Proving the old VPC serves the original backend at the preserved socket")
		rule := slrClient.Create(framework.MakeSwitchLBRule("move-"+suffix, f.Namespace.Name, vip, corev1.ServiceAffinityNone,
			movePathAnnotations(source), nil, []string{source.backend.Status.PodIP},
			[]fabricv1.SwitchLBRulePort{{Name: "http", Protocol: "TCP", Port: moveVIPPort, TargetPort: movePodPort}}))
		ginkgo.DeferCleanup(slrClient.DeleteSync, rule.Name)
		oldTCP := getLoadBalancerNameForVPC(source.vpc)
		newTCP := getLoadBalancerNameForVPC(target.vpc)
		controlTCP := getLoadBalancerNameForVPC(control.vpc)
		rows := moveVIPRows(f, source, target, control)
		controlBackend := util.JoinHostPort(control.backend.Status.PodIP, movePodPort)
		waitMoveVIPRows(rows, socket, map[string]string{
			oldTCP: util.JoinHostPort(source.backend.Status.PodIP, movePodPort), controlTCP: controlBackend,
		})
		waitMoveBackend(f.Namespace.Name, source.client, socket, source.backend.Name)
		waitMoveBackend(f.Namespace.Name, control.client, socket, control.backend.Name)

		ginkgo.By("Patching the same SwitchLBRule to use the target VPC and target backend")
		updated := rule.DeepCopy()
		updated.Annotations = movePathAnnotations(target)
		updated.Spec.Endpoints = []string{target.backend.Status.PodIP}
		current := slrClient.Patch(rule, updated)
		framework.ExpectEqual(current.UID, rule.UID)
		framework.ExpectEqual(current.Spec.Vip, vip)
		framework.ExpectEqual(current.Spec.Ports, rule.Spec.Ports)

		ginkgo.By("Checking that only the target and control TCP rows contain the exact socket")
		waitMoveVIPRows(rows, socket, map[string]string{
			newTCP: util.JoinHostPort(target.backend.Status.PodIP, movePodPort), controlTCP: controlBackend,
		})
		_, _, err := framework.NBExec("ovn-nbctl", "--timeout=30", "--wait=hv", "sync")
		framework.ExpectNoError(err)
		waitMoveBackend(f.Namespace.Name, target.client, socket, target.backend.Name)
		waitMoveBackend(f.Namespace.Name, control.client, socket, control.backend.Name)

		ginkgo.By("Confirming that the old client reaches its backend directly but cannot open the moved socket")
		waitMoveBackend(f.Namespace.Name, source.client, util.JoinHostPort(source.backend.Status.PodIP, movePodPort), source.backend.Name)
		for range 3 {
			probe, err := probeMoveSocket(f.Namespace.Name, source.client, socket)
			framework.ExpectNoError(err)
			framework.ExpectTrue(probe.connections == 0 && (probe.status == 7 || probe.status == 28),
				"expected curl to fail to connect from the old VPC, got exit %d and %d successful connections", probe.status, probe.connections)
		}
	})
})

func createMovePath(f *framework.Framework, name string) movePath {
	ginkgo.GinkgoHelper()
	path := movePath{vpc: "vpc-" + name, subnet: "subnet-" + name, client: "client-" + name}
	vpcClient := f.VpcClient()
	vpcClient.CreateSync(framework.MakeVpc(path.vpc, "", false, false, []string{f.Namespace.Name}))
	ginkgo.DeferCleanup(vpcClient.DeleteSync, path.vpc)
	subnetClient := f.SubnetClient()
	subnet := framework.MakeSubnet(path.subnet, "", framework.RandomCIDR(f.ClusterIPFamily), "", path.vpc, "", nil, nil, nil)
	subnet.Spec.EnableLb = new(true)
	subnetClient.CreateSync(subnet)
	ginkgo.DeferCleanup(subnetClient.DeleteSync, path.subnet)
	podClient := f.PodClient()
	podAnnotations := map[string]string{util.LogicalSwitchAnnotation: path.subnet}
	backendName := "backend-" + name
	path.backend = podClient.CreateSync(framework.MakePod(f.Namespace.Name, backendName, map[string]string{"app": backendName},
		podAnnotations, framework.AgnhostImage, []string{"/agnhost", "netexec", "--http-port", strconv.Itoa(int(movePodPort))}, nil))
	ginkgo.DeferCleanup(podClient.DeleteSync, path.backend.Name)
	podClient.CreateSync(framework.MakePod(f.Namespace.Name, path.client, nil, podAnnotations, framework.AgnhostImage, nil, nil))
	ginkgo.DeferCleanup(podClient.DeleteSync, path.client)
	return path
}

func movePathAnnotations(path movePath) map[string]string {
	return map[string]string{util.LogicalRouterAnnotation: path.vpc, util.LogicalSwitchAnnotation: path.subnet, util.ServiceHealthCheck: "false"}
}

func moveVIPRows(f *framework.Framework, paths ...movePath) []string {
	ginkgo.GinkgoHelper()
	rows := make([]string, 0, len(paths)*2)
	for _, path := range paths {
		vpc := f.VpcClient().Get(path.vpc)
		framework.ExpectEqual(vpc.Status.TCPLoadBalancer, getLoadBalancerNameForVPC(path.vpc))
		framework.ExpectEqual(vpc.Status.TCPSessionLoadBalancer, "vpc-"+path.vpc+"-tcp-sess-load")
		rows = append(rows, vpc.Status.TCPLoadBalancer, vpc.Status.TCPSessionLoadBalancer)
	}
	return rows
}

func waitMoveVIPRows(names []string, socket string, expected map[string]string) {
	ginkgo.GinkgoHelper()
	framework.WaitUntil(time.Second, 2*time.Minute, func(_ context.Context) (bool, error) {
		rows, err := readMoveVIPRows(names)
		if err != nil {
			return false, err
		}
		for _, name := range names {
			want, shouldExist := expected[name]
			got, exists := rows[name][socket]
			if exists != shouldExist || got != want {
				return false, nil
			}
		}
		return true, nil
	}, fmt.Sprintf("exact TCP load balancer entries for socket %s match %v", socket, expected))
}

func readMoveVIPRows(names []string) (map[string]map[string]string, error) {
	args := append([]string{"ovn-nbctl", "--timeout=10", "--format=json", "--columns=name,vips", "list", "Load_Balancer"}, names...)
	stdout, _, err := framework.NBExec(args...)
	if err != nil {
		return nil, err
	}
	var table struct {
		Headings []string            `json:"headings"`
		Data     [][]json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(stdout, &table); err != nil {
		return nil, err
	}
	if !slices.Equal(table.Headings, []string{"name", "vips"}) || len(table.Data) != len(names) {
		return nil, errors.New("the Northbound response does not contain all requested load balancers")
	}
	rows := make(map[string]map[string]string, len(names))
	for _, row := range table.Data {
		if len(row) != 2 {
			return nil, errors.New("the Northbound response contains a malformed load balancer row")
		}
		var name string
		var encodedMap []json.RawMessage
		if err := json.Unmarshal(row[0], &name); err != nil || !slices.Contains(names, name) || rows[name] != nil {
			return nil, errors.New("the Northbound response contains an unexpected load balancer name")
		}
		if err := json.Unmarshal(row[1], &encodedMap); err != nil || len(encodedMap) != 2 || string(encodedMap[0]) != `"map"` {
			return nil, fmt.Errorf("the Northbound response contains invalid VIPs for %s", name)
		}
		var pairs [][]string
		if err := json.Unmarshal(encodedMap[1], &pairs); err != nil {
			return nil, err
		}
		vips := make(map[string]string, len(pairs))
		for _, pair := range pairs {
			if len(pair) != 2 {
				return nil, fmt.Errorf("the Northbound response contains a malformed VIP entry for %s", name)
			}
			vips[pair[0]] = pair[1]
		}
		rows[name] = vips
	}
	return rows, nil
}

func waitMoveBackend(namespace, client, socket, backend string) {
	ginkgo.GinkgoHelper()
	framework.WaitUntil(time.Second, time.Minute, func(_ context.Context) (bool, error) {
		probe, err := probeMoveSocket(namespace, client, socket)
		return probe.status == 0 && probe.connections == 1 && probe.backend == backend, err
	}, fmt.Sprintf("%s gets the hostname %s through %s", client, backend, socket))
}

func probeMoveSocket(namespace, client, socket string) (moveProbe, error) {
	cmd := fmt.Sprintf(`code=0; reply=$(curl -q -fsS --noproxy '*' --connect-timeout 2 --max-time 3 -w '\n%%{num_connects}' %q 2>/dev/null) || code=$?; printf '%%s\n%%s\n' "$code" "$reply"`,
		"http://"+socket+"/hostname")
	stdout, _, err := framework.KubectlExec(namespace, client, cmd)
	if err != nil {
		return moveProbe{}, err
	}
	code, reply, found := strings.Cut(string(stdout), "\n")
	if !found {
		return moveProbe{}, errors.New("the curl probe did not report its exit status")
	}
	status, err := strconv.Atoi(strings.TrimSpace(code))
	if err != nil {
		return moveProbe{}, err
	}
	reply = strings.TrimSuffix(reply, "\n")
	separator := strings.LastIndex(reply, "\n")
	if separator < 0 {
		return moveProbe{}, errors.New("the curl probe did not report its successful connections")
	}
	connections, err := strconv.Atoi(strings.TrimSpace(reply[separator+1:]))
	return moveProbe{status: status, connections: connections, backend: strings.TrimSpace(reply[:separator])}, err
}
