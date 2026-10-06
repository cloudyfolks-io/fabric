package main

import (
	"os"
	"path/filepath"

	"github.com/cloudyfolks-io/fabric/cmd/pinger"
	"github.com/cloudyfolks-io/fabric/pkg/util"
	"github.com/cloudyfolks-io/fabric/pkg/util/profiling"
)

const (
	CmdController = "fabric-controller"
	CmdPinger     = "fabric-pinger"
)

func main() {
	cmd := filepath.Base(os.Args[0])
	switch cmd {
	case CmdController:
		profiling.DumpProfile()
		CmdMain()
	case CmdPinger:
		profiling.DumpProfile()
		pinger.CmdMain()
	default:
		util.LogFatalAndExit(nil, "%s is an unknown command", cmd)
	}
}
