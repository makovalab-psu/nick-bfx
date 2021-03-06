#!/usr/bin/env python3
import argparse
import configparser
import datetime
import getpass
import logging
import pathlib
import subprocess
import sys
import time
assert sys.version_info.major >= 3, 'Python 3 required'

def boolish(raw):
  if raw == True:
    return True
  elif raw == False or raw is None:
    return False
  elif raw.lower() in ('true', '1'):
    return True
  elif raw.lower() in ('false', '0'):
    return False
  else:
    return None

def csv(raw):
  if isinstance(raw, str):
    return raw.split(',')
  try:
    return list(raw)
  except TypeError:
    raise TypeError(f'Invalid comma-delimited list. Must give a string or a sequence. Saw {raw!r}.')

PARAMS = {
  'min_idle_nodes': {'type':int, 'default':0},
  'min_idle_cpus': {'type':int, 'default':0},
  'min_node_size': {'type':int, 'default':1},
  'min_node_size_cpus': {'type':int, 'fallback':'min_node_size'},
  'min_node_size_nodes': {'type':int, 'fallback':'min_node_size'},
  'max_jobs': {'type':int},
  'min_jobs': {'type':int, 'default':0},
  'prefer': {'type':str, 'default':'min'},
  'cpus': {'type':int, 'default':1},
  'mem': {'type':int, 'default':0},
  'stop': {'type':boolish, 'default':False},
  'pause': {'type':boolish, 'default':False},
  'affinity': {'type':csv},
}
PARAM_TYPES = {name:meta['type'] for name, meta in PARAMS.items()}
UNITS = {'B':1, 'K':1024, 'M':1024**2, 'G':1024**3, 'T':1024**4}
USER = getpass.getuser()
USAGE = """$ %(prog)s [parameters]
       $ %(prog)s -c config.ini"""
DESCRIPTION = """Determine whether we should keep launching slurm jobs, based on available
resources. Launch this and it will sleep until enough resources are available. Then, it will print
the name of a node with enough free CPUs to run your job."""


def make_argparser():
  parser = argparse.ArgumentParser(usage=USAGE, description=DESCRIPTION, add_help=False)
  options = parser.add_argument_group('Options')
  options.add_argument('-c', '--config', type=pathlib.Path,
    help='A config file to set all the parameters below. This will be read after every '
      '--check-interval, so you can update it while this script is running and it will change its '
      'behavior.')
  options.add_argument('-q', '--wait-for-job',
    help="Wait until the job with this name has begun. Useful if you just launched one and don't "
      "want to keep queueing jobs if they're not starting.")
  options.add_argument('-Q', '--wait-for-job-prefix',
    help='Same as --wait-for-job, but accept any job whose name starts with this string.')
  options.add_argument('-P', '--pause', action='store_true', default=None,
    help='Do not begin executing yet.')
  options.add_argument('-i', '--check-interval', type=int, default=15,
    help='How many seconds to wait between checks for available resources. Default: %(default)s')
  options.add_argument('--mock-sinfo', type=pathlib.Path)
  options.add_argument('-h', '--help', action='help',
    help='Print this argument help text and exit.')
  params = parser.add_argument_group('Parameters')
  params.add_argument('-C', '--cpus', type=int,
    help="How many CPUs are required by the job we're waiting to start. Default: "
      +str(PARAMS['cpus']['default']))
  params.add_argument('-M', '--mem', type=bytes_spec,
    help="How much memory is required by the job we're waiting to start. You can give a number "
      'ending "B", "K", "M", "G", or "T", case-insensitive. Default: '
      +str(PARAMS['mem']['default']))
  #TODO: Update help text with new, predictive algorithm.
  params.add_argument('-n', '--min-idle-nodes',
    help='Keep this many nodes idle: wait if only this many + 1 are idle.')
  params.add_argument('-u', '--min-idle-cpus',
    help='Keep this many CPUs idle: wait if only this many + --cpus are idle.')
  params.add_argument('-s', '--min-node-size',
    help="Minimum node size when counting available resources for the above thresholds. Don't "
      'consider nodes with fewer than this many CPUs.')
  params.add_argument('--min-node-size-cpus',
    help='Same as --min-node-size, but only for when counting idle cpus for --min-idle-cpus')
  params.add_argument('--min-node-size-nodes',
    help='Same as --min-node-size, but only for when counting idle nodes for --min-idle-nodes')
  params.add_argument('-J', '--max-jobs',
    help="Don't let yourself have more than this many jobs running at once. If you have this many "
      'jobs running, wait.')
  params.add_argument('-j', '--min-jobs',
    help='Always let yourself (try to) run at least this many jobs. Even if too few resources are '
      "available and you'd normally wait, keep going if fewer than this many jobs are running. "
      'In that case, this will exit but print nothing to stdout. Note: Does not override --pause.')
  params.add_argument('-a', '--affinity', default=(),
    help='The node(s) to prefer when choosing one to run on. Give a comma-separated list.')
  params.add_argument('-p', '--prefer', choices=('min', 'max'),
    help='Prefer nodes with either the most (max) or least (min) number of idle CPUs. '
      'Give --cpus to indicate how many CPUs the job requires. Only nodes with at least this '
      'many CPUs will be considered. Default: '+str(PARAMS['prefer']['default']))
  log = parser.add_argument_group('Logging')
  log.add_argument('-l', '--log', type=argparse.FileType('w'), default=sys.stderr,
    help='Print log messages to this file instead of to stderr. Warning: Will overwrite the file.')
  volume = log.add_mutually_exclusive_group()
  volume.add_argument('--quiet', dest='volume', action='store_const', const=logging.CRITICAL,
    default=logging.WARNING)
  volume.add_argument('-v', '--verbose', dest='volume', action='store_const', const=logging.INFO)
  volume.add_argument('-D', '--debug', dest='volume', action='store_const', const=logging.DEBUG)
  return parser


def main(argv):

  parser = make_argparser()
  args = parser.parse_args(argv[1:])

  logging.basicConfig(stream=args.log, level=args.volume, format='%(message)s')

  if args.config and not args.config.is_file():
    logging.warning(f'Warning: --config file {str(args.config)!r} not found!')

  if args.wait_for_job:
    if args.wait_for_job_prefix:
      fail('Error: Cannot give both --wait-for-job and --wait-for-job-prefix.')
    wait_for = args.wait_for_job
    prefixed = False
  else:
    wait_for = args.wait_for_job_prefix
    prefixed = True

  params = Parameters(args=args, config=args.config)

  if params.max_jobs is not None and params.min_jobs > params.max_jobs:
    fail(f'Error: --min-jobs must be <= --max-jobs ({params.min_jobs} > {params.max_jobs}).')

  node = None
  wait = True
  last_reason = None
  while wait or node is None:
    wait = False
    reason_msg = None
    if wait_for:
      if count_running_jobs(name=wait_for, prefixed=prefixed) or did_job_run(wait_for, prefixed):
        # It's running or has ran. Don't wait for it to start anymore now or in the future.
        wait_for = None
      else:
        reason_msg = f'Waiting for job {wait_for!r} to begin..'
        wait = True
    if params.max_jobs or params.min_jobs:
      running_jobs = count_running_jobs()
    if params.max_jobs and running_jobs >= params.max_jobs:
      reason_msg = f'Too many jobs running ({running_jobs} >= {params.max_jobs})'
      wait = True
    if params.pause:
      reason_msg = f'Execution paused.'
      wait = True
    states = get_node_states(mock_sinfo_path=args.mock_sinfo)
    node = choose_node(
      states,
      params.cpus,
      params.mem,
      min_idle_cpus=params.min_idle_cpus,
      min_idle_nodes=params.min_idle_nodes,
      min_node_size_cpus=params.min_node_size_cpus,
      min_node_size_nodes=params.min_node_size_nodes,
      affinities=params.affinity,
      chooser=params.prefer,
    )
    if node is None and reason_msg is None:
      reason_msg = f'No node currently fits the given constraints ({params})'
    if params.min_jobs and running_jobs < params.min_jobs and not params.pause:
      if wait or node is None:
        logging.warning(
          f"You're running fewer than {params.min_jobs} jobs. Ignoring limits and continuing."
        )
      break
    if wait or node is None:
      if reason_msg and reason_msg != last_reason:
        logging.warning(reason_msg)
      last_reason = reason_msg
      time.sleep(args.check_interval)
      if args.config:
        params = Parameters(args=args, config=args.config)
    if params.stop:
      logging.warning(f'Instructed to stop by {args.config}.')
      node = 'STOP'
      break

  logging.debug(f'wait: {wait!r}, node: {node!r}.')

  if node is not None:
    print(abbrev_node(node))


class Parameters:

  def __init__(self, args=None, config=None):
    # Initialize params.
    self.values = {}
    for name, meta in PARAMS.items():
      self.values[name] = None
    # Update with config file, if any.
    if config:
      self.update_with_config(config)
    # Update with args, if any.
    if args:
      self.update_with_args(args)
    self.set_defaults()

  def update_with_config(self, config_path):
    config = read_config_section(config_path, 'params', types=PARAM_TYPES)
    for name, value in config.items():
      if value is not None:
        self.values[name] = value

  def update_with_args(self, args):
    for name, meta in PARAMS.items():
      raw_value = getattr(args, name, None)
      if raw_value is not None:
        value = meta['type'](raw_value)
        self.values[name] = value

  def __getattr__(self, name):
    value = self.values.get(name)
    if value is not None:
      return value
    else:
      return None

  def set_defaults(self):
    """Set any unset params to their defaults (or the defaults of their fallbacks)."""
    for name, meta in PARAMS.items():
      if self.values[name] is not None:
        continue
      if 'default' in meta:
        self.values[name] = meta['default']
      elif 'fallback' in meta:
        fallback = meta['fallback']
        self.values[name] = self.values[fallback]

  @staticmethod
  def get_default(name):
    default_value = None
    if 'fallback' in PARAMS[name]:
      fallback = PARAMS[name]['fallback']
      if 'default' in PARAMS[fallback]:
        default_value = PARAMS[fallback]['default']
      elif 'default' in PARAMS[name]:
        default_value = PARAMS[name]
    elif 'default' in PARAMS[name]:
      default_value = PARAMS[name]
    return default_value

  def subdivide_param(self, main, specifics):
    """Use the value of parameter `main` as the default value for parameters `specifics`.
    Deprecated."""
    for specific in specifics:
      if self.values[specific] is None:
        self.values[specific] = self.values[main]

  def __str__(self):
    param_strs = []
    for name, value in self.values.items():
      default = self.get_default(name)
      if value is not None and value != default:
        param_strs.append(f'{name}: {value!r}')
    return ', '.join(param_strs)


def read_config_section(config_path, section, types=None):
  data = {}
  config = configparser.ConfigParser(interpolation=None)
  try:
    config.read(config_path)
    for key, raw_value in config.items(section):
      if types and key in types:
        value = types[key](raw_value)
      else:
        value = raw_value
      data[key] = value
  except configparser.Error as error:
    fail(f'Invalid config file format in {config_path!r}: {error}')
  return data


def parse_file_or_value(raw_value, coerce_type):
  if raw_value is None:
    return None, None
  try:
    return coerce_type(raw_value), None
  except ValueError:
    path = pathlib.Path(raw_value)
  if not path.is_file():
    fail(f'Error: Argument {raw_value!r} not a {coerce_type.__name__} or existing file.')
  return None, path


def get_node_states(mock_sinfo_path=None):
  states = {}
  cmd = (
    'sinfo', '--noheader', '--Node', '--partition', 'general', '--states', 'idle,alloc',
    '--Format', 'nodelist,memory,allocmem,cpusstate',
  )
  if mock_sinfo_path:
    stdout = mock_sinfo_path.open().read()
  else:
    stdout = run_command(cmd, 'Error: Problem getting CPU usage info.')
  for line in stdout.splitlines():
    major_fields = line.split()
    if len(major_fields) != 4:
      logging.warning(
        f'Warning: sinfo line has wrong number of fields ({len(major_fields)}): {line!r}'
      )
      continue
    node_name = major_fields[0]
    total_mem_str = major_fields[1]
    alloc_mem_str = major_fields[2]
    minor_fields = major_fields[3].split('/')
    mem_str = major_fields[2]
    if len(minor_fields) != 4:
      logging.warning(
        f'Warning: sinfo line has wrong number of cpu fields ({len(minor_fields)}): {line!r}'
      )
      continue
    try:
      node_idle = int(minor_fields[1])
      node_size = int(minor_fields[3])
    except ValueError:
      logging.warning(
        f'Warning: sinfo line has invalid cpu fields ({minor_fields[1]!r} or {minor_fields[3]!r}): '
        f'{line!r}'
      )
      continue
    try:
      total_mem = int(major_fields[1]) * 1024**2
      alloc_mem = int(major_fields[2]) * 1024**2
    except ValueError:
      logging.warning(
        f'Warning: sinfo line has invalid memory values ({major_fields[1]!r} and/or '
        f'{major_fields[2]!r}): {line!r}.'
      )
      continue
    free_mem = total_mem - alloc_mem
    states[node_name] = {'name':node_name, 'idle':node_idle, 'cpus':node_size, 'mem':free_mem}
  return states


def choose_node(
    states,
    job_cpus,
    job_mem,
    min_idle_cpus=0,
    min_idle_nodes=0,
    min_node_size_cpus=1,
    min_node_size_nodes=1,
    affinities=(),
    chooser=max,
  ):
  """Choose a node to run the job on, if any.
  If the resources the job would consume would make them fall below the given thresholds, return
  `None`.
  `chooser`: Whether to prefer nodes with more or less free CPUs. `max` will make it prefer nodes
  with the most idle CPUs, spreading your jobs out across nodes. `min` will make it prefer nodes
  with fewer available CPUs (but still enough to run the job)."""
  chooser = get_chooser(chooser)
  idle_nodes, idle_cpus = count_idle_resources(
    states,
    min_node_size_cpus=min_node_size_cpus,
    min_node_size_nodes=min_node_size_nodes,
  )
  if idle_cpus - job_cpus < min_idle_cpus:
    return None
  if idle_nodes - 1 < min_idle_nodes:
    exclude_idle_nodes = True
  else:
    exclude_idle_nodes = False
  # Narrow it down to eligible nodes.
  candidates = []
  for node in states.values():
    if node['cpus'] < min_node_size_cpus:
      continue
    if node['idle'] < job_cpus:
      continue
    if node['mem'] < job_mem:
      continue
    if node['idle'] == node['cpus'] and exclude_idle_nodes:
      continue
    candidates.append(node)
  # Choose from among the eligible nodes.
  best_node = None
  for node in candidates:
    if node['name'] in affinities:
      best_node = node
      break
    if best_node is None:
      best_node = node
    else:
      result = chooser(node['idle'], best_node['idle'])
      if result != best_node['idle']:
        best_node = node
  if best_node is None:
    return None
  else:
    return best_node['name']


def count_idle_resources(states, min_node_size_cpus=1, min_node_size_nodes=1):
  idle_nodes = 0
  idle_cpus = 0
  for node in states.values():
    if node['cpus'] >= min_node_size_cpus:
      idle_cpus += node['idle']
    if node['cpus'] >= min_node_size_nodes:
      if node['idle'] == node['cpus']:
        idle_nodes += 1
  return idle_nodes, idle_cpus


def get_chooser(chooser_raw):
  if hasattr(chooser_raw, '__call__'):
    return chooser_raw
  else:
    if chooser_raw == 'min':
      return min
    elif chooser_raw == 'max':
      return max
    else:
      fail(f'Error: Invalid chooser {chooser_raw!r}')


def count_running_jobs(name=None, prefixed=False):
  jobs = 0
  cmd = ('squeue', '-h', '-u', USER, '-t', 'running,configuring,pending', '-o', '%j')
  stdout = run_command(cmd, 'Problem getting a list of running jobs.')
  for line in stdout.splitlines():
    if name is None:
      jobs += 1
    else:
      if prefixed:
        if line.startswith(name):
          jobs += 1
      else:
        if line == name:
          jobs += 1
  return jobs


def did_job_run(name, prefixed=None, job_history=None):
  if job_history is None:
    job_history = get_job_history()
  if prefixed:
    for candidate in job_history.keys():
      if candidate.startswith(name):
        return True
  else:
    return job_history.get(name)


def get_job_history(age=2*60):
  """Look back at old (and current) jobs and compile the state of historical jobs.
  Only look at jobs started in the last `age` seconds."""
  job_history = {}
  times = {}
  since = time.time() - age
  since_dt = datetime.datetime.fromtimestamp(since)
  since_str = since_dt.strftime('%Y-%m-%dT%H:%M:%S')
  cmd = ('sacct', '-n', '--starttime', since_str, '--format=Start%20,Jobname%30,state%20')
  stdout = run_command(cmd, 'Problem getting sacct list of job history.')
  for line in stdout.splitlines():
    start = line[:20].strip()
    job_name = line[20:51].strip()
    state_raw = line[51:].strip()
    if state_raw.startswith('CANCELLED by '):
      state = 'CANCELLED'
    else:
      state = state_raw
    existing_state = job_history.get(job_name)
    existing_time = times.get(job_name)
    if existing_time is None or start > existing_time:
      job_history[job_name] = state
      times[job_name] = start
  return job_history


def abbrev_node(node_name):
  fields = node_name.split('.')
  return fields[0]


def bytes_spec(bytes_str):
  quantity_str = bytes_str[:len(bytes_str)-1]
  unit = bytes_str[-1].upper()
  # Possible ValueError to be caught by caller.
  quantity = int(quantity_str)
  if unit not in UNITS:
    raise ValueError(f'Invalid unit in byte amount {bytes_str!r}.')
  multiplier = UNITS[unit]
  return quantity * multiplier


def run_command(cmd, message):
  logging.info('Info: Running $ '+' '.join(map(str, cmd)))
  result = subprocess.run(cmd, encoding='utf8', stderr=subprocess.PIPE, stdout=subprocess.PIPE)
  if result.returncode != 0:
    sys.stderr.write(result.stderr)+'\n'
    fail('Error: '+message)
  return result.stdout


def read_file(path, coerce_type=None):
  with path.open('rt') as file:
    for line_raw in file:
      line = line_raw.strip()
      if coerce_type:
        return coerce_type(line)
      else:
        return line


def fail(message):
  logging.critical(message)
  if __name__ == '__main__':
    sys.exit(1)
  else:
    raise Exception('Unrecoverable error')


if __name__ == '__main__':
  try:
    sys.exit(main(sys.argv))
  except (BrokenPipeError, KeyboardInterrupt):
    pass
