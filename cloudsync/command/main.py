import sys
import argparse
import logging
import importlib
from typing import Any

from .utils import SubCmd

logging.basicConfig(format='%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
        datefmt='%Y-%m-%d:%H:%M:%S',)
log = logging.getLogger()

def main():
    """cloudsync command line main"""

    parser = argparse.ArgumentParser(description='cloudsync - monitor and sync between cloud providers')
    cmds = parser.add_subparsers(title="Commands")

    cmds.metavar = "Commands:"
    sub_cmds = ["debug", "sync", "list"]
    for sub_cmd in sub_cmds:
        module: Any = importlib.import_module(".." + sub_cmd, __name__)
        cmd: SubCmd = module.cmd_class(cmds)

        cmd.parser.add_argument('-v', '--verbose', help='More verbose logging', action="store_true")
        cmd.parser.set_defaults(func=cmd.run)

    args = parser.parse_args()

    log.setLevel(logging.INFO)
    if args.verbose:
        log.setLevel(logging.DEBUG)
        print("# args", args.__dict__, file=sys.stderr)

    if "func" not in args:
        parser.print_help(file=sys.stderr)
        sys.exit(1)

    try:
        args.func(args)
    except Exception as e:
        if args.verbose:
            log.exception("error running command")
        print("Error ", e, file=sys.stderr)
