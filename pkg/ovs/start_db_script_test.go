package ovs

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestStartDBAddressFunctions(t *testing.T) {
	bash := requireBash(t)

	_, filename, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("failed to get current filename")
	}

	for _, script := range []struct {
		name      string
		path      string
		endMarker string
	}{
		{
			name:      "start-db",
			path:      filepath.Join(filepath.Dir(filename), "..", "..", "dist", "images", "start-db.sh"),
			endMarker: "function get_leader_addr",
		},
		{
			name:      "start-ic-db",
			path:      filepath.Join(filepath.Dir(filename), "..", "..", "dist", "images", "start-ic-db.sh"),
			endMarker: "function ovn_db_pre_start",
		},
	} {
		t.Run(script.name, func(t *testing.T) {
			content, err := os.ReadFile(script.path)
			if err != nil {
				t.Fatalf("failed to read %s: %v", script.path, err)
			}

			functions := extractStartDBAddressFunctions(t, string(content), script.endMarker)

			tests := []struct {
				name     string
				command  string
				expected string
			}{
				{
					name:     "hostname connection address",
					command:  "ENABLE_SSL=false; gen_conn_addr ovn-central-0.ovn-central.hcp.svc.cluster.local 6643",
					expected: "tcp:ovn-central-0.ovn-central.hcp.svc.cluster.local:6643",
				},
				{
					name:     "ssl hostname connection address",
					command:  "ENABLE_SSL=true; gen_conn_addr ovn-central-0.ovn-central.hcp.svc.cluster.local 6643",
					expected: "ssl:ovn-central-0.ovn-central.hcp.svc.cluster.local:6643",
				},
				{
					name:     "ipv4 connection address",
					command:  "ENABLE_SSL=false; gen_conn_addr 10.3.0.58 6643",
					expected: "tcp:[10.3.0.58]:6643",
				},
				{
					name:     "ipv6 connection address",
					command:  "ENABLE_SSL=false; gen_conn_addr fd00::58 6643",
					expected: "tcp:[fd00::58]:6643",
				},
				{
					name:     "hostname connection string",
					command:  "ENABLE_SSL=false; NODE_IPS='ovn-central-0.ovn-central.hcp.svc.cluster.local, ovn-central-1.ovn-central.hcp.svc.cluster.local'; gen_conn_str 6641",
					expected: "tcp:ovn-central-0.ovn-central.hcp.svc.cluster.local:6641,tcp:ovn-central-1.ovn-central.hcp.svc.cluster.local:6641",
				},
			}

			for _, tt := range tests {
				t.Run(tt.name, func(t *testing.T) {
					output, err := exec.Command(bash, "-c", functions+"\n"+tt.command).CombinedOutput() // #nosec G204
					if err != nil {
						t.Fatalf("command failed: %v\noutput: %s", err, output)
					}

					got := strings.TrimSpace(string(output))
					if got != tt.expected {
						t.Fatalf("expected %q, got %q", tt.expected, got)
					}
				})
			}
		})
	}
}

func TestStartDBClusterAddressArgsUseFormatter(t *testing.T) {
	_, filename, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("failed to get current filename")
	}

	scriptPath := filepath.Join(filepath.Dir(filename), "..", "..", "dist", "images", "start-db.sh")
	content, err := os.ReadFile(scriptPath)
	if err != nil {
		t.Fatalf("failed to read %s: %v", scriptPath, err)
	}
	script := string(content)

	for _, oldArg := range []string{
		"--db-nb-cluster-local-addr=[$DB_CLUSTER_ADDR]",
		"--db-sb-cluster-local-addr=[$DB_CLUSTER_ADDR]",
		"--db-nb-cluster-remote-addr=[$nb_leader_addr]",
		"--db-sb-cluster-remote-addr=[$sb_leader_addr]",
	} {
		if strings.Contains(script, oldArg) {
			t.Fatalf("cluster address argument %q should use format_ovsdb_addr", oldArg)
		}
	}
}

func TestStartDBExpandsSvcAddressesFromDNSResponse(t *testing.T) {
	bash := requireBash(t)

	_, filename, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("failed to get current filename")
	}

	scriptPath := filepath.Join(filepath.Dir(filename), "..", "..", "dist", "images", "start-db.sh")
	content, err := os.ReadFile(scriptPath)
	if err != nil {
		t.Fatalf("failed to read %s: %v", scriptPath, err)
	}

	functions := extractStartDBAddressFunctions(t, string(content), "function gen_conn_addr")
	command := strings.Join([]string{
		`function getent { case "$2" in ovn-central-0.ovn-central.hcp.svc) echo "10.3.0.67 ovn-central-0.ovn-central.hcp.svc.acme.local" ;; ovn-central-1.ovn-central.hcp.svc) echo "10.3.0.68 ovn-central-1.ovn-central.hcp.svc.acme.local" ;; esac; }`,
		"POD_NAMESPACE=hcp",
		"DB_CLUSTER_ADDR=ovn-central-0.ovn-central.hcp.svc",
		"POD_IP=$DB_CLUSTER_ADDR",
		"NODE_IPS=ovn-central-0.ovn-central.hcp.svc,ovn-central-1.ovn-central.hcp.svc",
		"normalize_raft_addrs",
		"printf '%s\\n%s\\n%s\\n' \"$DB_CLUSTER_ADDR\" \"$POD_IP\" \"$NODE_IPS\"",
	}, "; ")

	output, err := exec.Command(bash, "-c", functions+"\n"+command).CombinedOutput() // #nosec G204
	if err != nil {
		t.Fatalf("command failed: %v\noutput: %s", err, output)
	}

	expected := strings.Join([]string{
		"ovn-central-0.ovn-central.hcp.svc.acme.local",
		"ovn-central-0.ovn-central.hcp.svc.acme.local",
		"ovn-central-0.ovn-central.hcp.svc.acme.local,ovn-central-1.ovn-central.hcp.svc.acme.local",
	}, "\n")
	if got := strings.TrimSpace(string(output)); got != expected {
		t.Fatalf("expected %q, got %q", expected, got)
	}
}

func TestStartDBInitializesNorthboundCompatibility(t *testing.T) {
	bash := requireBash(t)
	_, filename, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("failed to get current filename")
	}
	content, err := os.ReadFile(filepath.Join(filepath.Dir(filename), "..", "..", "dist", "images", "start-db.sh"))
	if err != nil {
		t.Fatal(err)
	}
	script := string(content)
	_, compatibility, found := strings.Cut(script, "function set_nb_version_compatibility() {")
	if !found {
		t.Fatal("the Northbound compatibility function is missing")
	}
	compatibility, _, found = strings.Cut(compatibility, "\n}\n")
	if !found {
		t.Fatal("the Northbound compatibility function is incomplete")
	}
	_, startup, found := strings.Cut(script, "trap quit EXIT\n")
	if !found {
		t.Fatal("the database startup block is missing")
	}
	startup, _, found = strings.Cut(startup, "\novn-appctl -t /var/run/ovn/ovnnb_db.ctl ovsdb-server/memory-trim-on-compaction on")
	if !found {
		t.Fatal("the database startup block is incomplete")
	}
	startup = strings.ReplaceAll(startup, "/usr/share/ovn/scripts/ovn-ctl", "ovn_ctl")
	functions := extractStartDBAddressFunctions(t, script, "function get_leader_addr")
	stubs := `
set -eo pipefail
DB_CLUSTER_ADDR=192.0.2.10
NB_PORT=7641
SB_PORT=7642
DB_ADDR=::
SSL_OPTIONS=
if [[ "$ENABLE_SSL" == "true" ]]; then SSL_OPTIONS="-p test-key -c test-cert -C test-ca"; fi
function rm { :; }
function get_leader_addr { echo "$DB_CLUSTER_ADDR"; }
function is_clustered { return 1; }
function ovn_db_pre_start { :; }
function ovn_db_ssl_args { :; }
function ovn-sbctl { :; }
function ovn_ctl {
    case "${!#}" in
        restart_northd|start_northd) echo "NORTHD ${!#}" ;;
    esac
}
function ovn-nbctl {
    case "${!#}" in
        options)
            if [[ "$CURRENT_COMPATIBILITY" == "unavailable" ]]; then return 5; fi
            if [[ -n "$CURRENT_COMPATIBILITY" ]]; then printf '{version_compatibility="%s"}\n' "$CURRENT_COMPATIBILITY"; else echo '{}'; fi
            ;;
        options:version_compatibility) printf '"%s"\n' "$CURRENT_COMPATIBILITY" ;;
        options:version_compatibility=*) printf 'WRITE %s\n' "$*" ;;
    esac
}
`
	cases := []struct {
		name, ssl, nodes, previous, write string
		failed                            bool
	}{
		{name: "plain standalone", ssl: "false", write: "WRITE set NB_Global . options:version_compatibility=25.03"},
		{name: "TLS standalone", ssl: "true", write: "WRITE set NB_Global . options:version_compatibility=25.03"},
		{name: "TLS clustered bootstrap", ssl: "true", nodes: "192.0.2.10,192.0.2.11", write: "WRITE --db=ssl:[192.0.2.10]:7641,ssl:[192.0.2.11]:7641 -p test-key -c test-cert -C test-ca set NB_Global . options:version_compatibility=25.03"},
		{name: "already at the target version", ssl: "true", previous: "25.03"},
		{name: "already completed the upgrade", ssl: "true", previous: "_25.03"},
		{name: "unavailable Northbound", ssl: "true", previous: "unavailable", failed: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cmd := exec.Command(bash, "-c", stubs+functions+"\nfunction set_nb_version_compatibility() {"+compatibility+"\n}\n"+startup)
			cmd.Env = append(os.Environ(), "ENABLE_SSL="+tc.ssl, "NODE_IPS="+tc.nodes, "CURRENT_COMPATIBILITY="+tc.previous, "OVN_VERSION_COMPATIBILITY=25.03")
			output, err := cmd.CombinedOutput()
			if (err != nil) != tc.failed {
				t.Fatalf("unexpected startup result %v\n%s", err, output)
			}
			var writes []string
			for line := range strings.SplitSeq(string(output), "\n") {
				if strings.HasPrefix(line, "WRITE ") {
					writes = append(writes, line)
				}
			}
			if strings.Join(writes, "\n") != tc.write {
				t.Fatalf("expected %q, got %q\n%s", tc.write, writes, output)
			}
			if tc.nodes != "" && strings.Index(string(output), tc.write) >= strings.Index(string(output), "NORTHD start_northd") {
				t.Fatalf("Northbound compatibility must be set before northd starts\n%s", output)
			}
		})
	}
}

func requireBash(t *testing.T) string {
	t.Helper()

	bash, err := exec.LookPath("bash")
	if err != nil {
		t.Skip("bash is required to test start-db.sh snippets")
	}
	return bash
}

func extractStartDBAddressFunctions(t *testing.T, script, endMarker string) string {
	t.Helper()

	start := strings.Index(script, "function format_ovsdb_addr")
	if start == -1 {
		start = firstFunctionIndex(script, "function gen_conn_addr", "function gen_conn_str")
	}
	if start == -1 {
		t.Fatal("failed to find address functions")
	}
	end := strings.Index(script[start:], endMarker)
	if end == -1 {
		t.Fatalf("failed to find %s", endMarker)
	}
	return script[start : start+end]
}

func firstFunctionIndex(script string, markers ...string) int {
	first := -1
	for _, marker := range markers {
		index := strings.Index(script, marker)
		if index != -1 && (first == -1 || index < first) {
			first = index
		}
	}
	return first
}
