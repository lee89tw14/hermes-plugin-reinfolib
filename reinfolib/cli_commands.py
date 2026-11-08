"""Terminal-only maintenance command: `hermes jp-reinfolib delete <snapshot|export> <uuid>`.

Registered through ctx.register_cli_command only, so gateway/chat surfaces cannot dispatch it.
Anyone who can run commands on the host as this user (including an agent with a terminal tool)
can still run it; it is a convenience, not a permission boundary.
"""
import json
import sys

from .storage import CoreError

NAME = 'jp-reinfolib'
HELP = 'jp-reinfolib local maintenance (terminal only)'
DESCRIPTION = ('Delete one saved v2 snapshot or export set (JSON/CSV or raw PBF tile, with its '
               'metadata) early. Snapshots and exports are also deleted automatically after 30 days.')


def setup(parser):
    subs = parser.add_subparsers(dest='reinfolib_command')
    delete = subs.add_parser('delete', help='Delete one snapshot or export set by UUID',
                             description='Delete exactly one artifact; snapshots and exports never cascade.')
    delete.add_argument('kind', help='snapshot or export')
    delete.add_argument('artifact_id', help='32 lowercase hex characters')
    delete.add_argument('--yes', action='store_true', help='Confirm without an interactive prompt')
    parser.set_defaults(func=handle)


def _fail(error):
    print(f'error: {error.error_type}/{error.detail}', file=sys.stderr)
    return 2 if error.error_type == 'invalid_argument' else 1


def handle(args, *, data_root=None, ask=None, interactive=None):
    from . import maintenance
    if getattr(args, 'reinfolib_command', None) != 'delete':
        print('usage: hermes jp-reinfolib delete <snapshot|export> <uuid> [--yes]', file=sys.stderr)
        return 2
    try:
        maintenance.validate_artifact_ref(args.kind, args.artifact_id)
        confirmed = args.yes is True
        if not confirmed:
            if interactive is None:
                interactive = sys.stdin.isatty()
            if interactive:
                ask = ask or input
                try:
                    answer = ask(f'Delete {args.kind} {args.artifact_id}? [y/N] ')
                except EOFError:
                    answer = ''
                confirmed = answer.strip().lower() in ('y', 'yes')
        if not confirmed:
            raise CoreError('invalid_argument', 'confirmation_required')
        if data_root is None:
            from .adapter import data_root
        result = maintenance.delete_artifact(data_root(), kind=args.kind,
                                             artifact_id=args.artifact_id, confirmed=True)
    except CoreError as error:
        return _fail(error)
    except OSError:
        return _fail(CoreError('storage', 'storage_unavailable'))
    print(json.dumps(result, ensure_ascii=False))
    return 0
