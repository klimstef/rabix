import os
import docopt
import sys
import logging
import six

from rabix import __version__ as version
from rabix.common.util import set_log_level, dot_update_dict
from rabix.common.models import Job, IO
from rabix.common.context import Context
from rabix.common.ref_resolver import from_url
from rabix.cli.adapter import CLIJob
from rabix.executor import Executor
from rabix.cli import CliApp

import rabix.cli
import rabix.docker
import rabix.expressions
import rabix.workflows
import rabix.schema


TEMPLATE_RESOURCES = {
    "cpu": 4,
    "mem": 5000,
    "diskSpace": 20000,
    "network": False
}


TEMPLATE_JOB = {
    'app': 'http://example.com/app.json',
    'inputs': {},
    'platform': 'http://example.org/my_platform/v1',
    'allocatedResources': {

    }
}

USAGE = '''
Usage:
    rabix <tool> [-v...] [-hcI] [-d <dir>] [-i <inp>] [{resources}] [-- {inputs}...]
    rabix --version

    Options:
  -d --dir=<dir>       Working directory for the task. If not provided one will
                       be auto generated in the current dir.
  -h --help            Show this help message. In conjunction with tool,
                       it will print inputs you can provide for the job.

  -I --install         Only install referenced tools. Do not run anything.
  -i --inp-file=<inp>  Inputs
  -c --print-cli       Only print calculated command line. Do not run anything.
  -v --verbose         Verbosity. More Vs more output.
     --version         Print version and exit.
'''

TOOL_TEMPLATE = '''
Usage:
  tool {inputs}
'''


def make_resources_usage_string(template=TEMPLATE_RESOURCES):
    param_str = []
    for k, v in six.iteritems(template):
        if type(v) is bool:
            arg = '--resources.%s' % k
        else:
            arg = '--resources.%s=<%s>' % (k, type(v).__name__)
        param_str.append(arg)
    return ' '.join(param_str)


TYPE_MAP = {
    'Job': Job.from_dict,
    'IO': IO.from_dict
}


def init_context():
    executor = Executor()
    context = Context(TYPE_MAP, executor)

    for module in (
            rabix.cli, rabix.expressions, rabix.workflows,
            rabix.schema, rabix.docker
    ):
        module.init(context)

    return context


def fix_types(tool):
    requirements = tool.get('requirements', {})
    environment = requirements.get('environment')

    # container type
    if (environment and
            isinstance(environment.get('container'), dict) and
            environment['container'].get('type') == 'docker'):
        environment['container']['@type'] = 'Docker'

    # tool type
    if '@type' not in tool:
        tool['@type'] = 'CommandLineTool'

    if tool['@type'] == 'Workflow':
        for step in tool['steps']:
            fix_types(step['app'])

    # schema type
    inputs = tool.get('inputs')
    if isinstance(inputs, dict) and '@type' not in inputs:
        inputs['@type'] = 'JsonSchema'

    outputs = tool.get('outputs')
    if isinstance(outputs, dict) and '@type' not in outputs:
        outputs['@type'] = 'JsonSchema'


def make_app_usage_string(app, template=TOOL_TEMPLATE, inp=None):

    inp = inp or {}

    def required(req, arg, inputs):
        inp = inputs.keys()
        if (arg in req) and (arg not in inp):
            return True
        return False

    def resolve(k, v, req, usage_str, param_str, inp):
        if v.get('type') == 'array':
            if v.get('items').get('type') == 'object':
                pass
            elif ((v.get('items').get('type') == 'file' or v.get(
                    'items').get('type') == 'directory')):
                arg = '--%s=<file>...' % k
                usage_str.append(arg if required(req, k, inp)
                                 else '[%s]' % arg)
            else:
                arg = '--%s=<array_%s_separator(%s)>...' % (
                    k, v.get('items', {}).get('type'),
                    v.get('adapter', {}).get('itemSeparator')
                )
                param_str.append(arg if required(req, k, inp)
                                 else '[%s]' % arg)
        elif v.get('type') == 'file':
            arg = '--%s=<file>' % k
            usage_str.append(arg if required(req, k, inp)
                             else '[%s]' % arg)
        else:
            arg = '--%s=<%s>' % (k, v.get('type'))
            param_str.append(arg if required(req, k, inp)
                             else '[%s]' % arg)

    def resolve_object(name, obj, usage_str, param_str, inp, root=False):
        properties = obj.get('properties', {})
        required = obj.get('required', [])
        for k, v in six.iteritems(properties):
            key = k if root else '.'.join([name, k])
            resolve(key, v, required, usage_str, param_str, inp)

    inputs = app.inputs.schema
    usage_str = []
    param_str = []

    resolve_object('inputs', inputs, usage_str, param_str, inp, root=True)
    usage_str.extend(param_str)
    return template.format(resources=make_resources_usage_string(),
                           inputs=' '.join(usage_str))


def resolve_values(k, v, nval, inputs, startdir=None):
    if isinstance(nval, list):
        if v.get('type') != 'array':
            raise Exception('Too many values')
        inputs[k] = []
        for nv in nval:
            if (v['items']['type'] == 'file' or v['items'][
                    'type'] == 'directory'):
                if startdir:
                    nv = os.path.join(startdir, nv)
                inputs[k].append({'path': nv})
            else:
                inputs[k].append(nv)
    else:
        if v['type'] == 'file' or v['type'] == 'directory':
            if startdir:
                nval = os.path.join(startdir, nval)
            inputs[k] = {'path': nval}
        elif v['type'] == 'integer':
            inputs[k] = int(nval)
        elif v['type'] == 'number':
            inputs[k] = float(nval)
        else:
            inputs[k] = nval


def get_inputs_from_file(tool, args, startdir):
    inp = {}
    inputs = tool.get('inputs', {}).get('properties')  # for inputs
    resolve_nested_paths(inp, inputs, args, startdir)
    return {'inputs': inp}


def resolve_nested_paths(inp, inputs, args, startdir):
    for k, v in six.iteritems(inputs):
        nval = args.get(k)
        if nval:
            if (v.get('type') == 'array' and
                    v.get('items', {}).get('type') == 'object'):  # for inner objects
                inp[k] = []
                for sk, sv in enumerate(nval):
                    inp[k].append({})
                    resolve_nested_paths(
                        inp[k][sk],
                        inputs[k].get('items').get('properties'),
                        v, startdir
                    )
            else:
                resolve_values(k, v, nval, inp, startdir)


def get_inputs(app, args):
    inputs = {}
    properties = app.inputs.schema['properties']
    for k, v in six.iteritems(properties):
        nval = args.get('--' + k) or args.get(k)
        if nval:
            resolve_values(k, v, nval, inputs)
    return {'inputs': inputs}


def update_paths(job, inputs):
    for inp in inputs['inputs'].keys():
        job['inputs'][inp] = inputs['inputs'][inp]
    return job


def get_tool(args):
    if args['<tool>']:
        return from_url(args['<tool>'])


def dry_run_parse(args=None):
    args = args or sys.argv[1:]
    args += ['an_input']
    usage = USAGE.format(resources=make_resources_usage_string(),
                         inputs='<inputs>')
    try:
        return docopt.docopt(usage, args, version=version, help=False)
    except docopt.DocoptExit:
        return


def main():
    logging.basicConfig(level=logging.WARN)
    if len(sys.argv) == 1:
        print(USAGE)
        return

    usage = USAGE.format(resources=make_resources_usage_string(),
                         inputs='<inputs>')
    app_usage = usage

    if len(sys.argv) == 2 and \
            (sys.argv[1] == '--help' or sys.argv[1] == '-h'):
        print(USAGE)
        return

    dry_run_args = dry_run_parse()
    if not dry_run_args:
        print(USAGE)
        return

    if not (dry_run_args['<tool>']):
        print('You have to specify a tool, with --tool option')
        print(usage)
        return

    tool = get_tool(dry_run_args)
    if not tool:
        print("Couldn't find tool.")
        return

    fix_types(tool)

    context = init_context()
    app = context.from_dict(tool)

    if dry_run_args['--install']:
        app.install()
        print("Install successful.")
        return

    try:
        args = docopt.docopt(usage, version=version, help=False)
        job = TEMPLATE_JOB
        set_log_level(dry_run_args['--verbose'])

        if args['--inp-file']:
            startdir = os.path.dirname(args.get('--inp-file'))
            input_file = from_url(args.get('--inp-file'))
            dot_update_dict(
                job['inputs'],
                get_inputs_from_file(tool, input_file, startdir)['inputs']
            )

        app_inputs_usage = make_app_usage_string(
            app, template=TOOL_TEMPLATE, inp=job['inputs'])
        app_usage = make_app_usage_string(app, USAGE, job['inputs'])

        app_inputs = docopt.docopt(app_inputs_usage, args['<inputs>'])

        if args['--help']:
            print(app_usage)
            return

        inp = get_inputs(app, app_inputs)
        job = update_paths(job, inp)

        if args['--print-cli']:
            if not isinstance(app, CliApp):
                print(dry_run_args['<tool>'] + " is not a command line app")
                return
            adapter = CLIJob(job, tool)
            print(adapter.cmd_line())
            return

        job['@id'] = args.get('--dir')
        job['app'] = app.to_dict()
        print(app.run(Job.from_dict(context, job)))

    except docopt.DocoptExit:
        print(app_usage)
        return


if __name__ == '__main__':
    main()
