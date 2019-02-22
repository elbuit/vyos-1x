#!/usr/bin/env python3
#
# Copyright (C) 2018 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#

"""
conf-mode script for 'system host-name' and 'system domain-name'.
"""

import os
import re
import sys
import subprocess
import copy
import jinja2
import glob

from vyos.config import Config
from vyos import ConfigError

config_file_hosts = '/etc/hosts'
config_file_resolv = '/etc/resolv.conf'

config_tmpl_hosts = """
### Autogenerated by host_name.py ###
127.0.0.1       localhost {{ hostname }}{% if domain_name %}.{{ domain_name }}{% endif %}

# The following lines are desirable for IPv6 capable hosts
::1             localhost ip6-localhost ip6-loopback
fe00::0         ip6-localnet
ff00::0         ip6-mcastprefix
ff02::1         ip6-allnodes
ff02::2         ip6-allrouters

# static hostname mappings
{%- if static_host_mapping['hostnames'] %}
{% for hn in static_host_mapping['hostnames'] -%}
{{static_host_mapping['hostnames'][hn]['ipaddr']}}\t{{static_host_mapping['hostnames'][hn]['alias']}}\t{{hn}}
{% endfor -%}
{%- endif %}

### modifications from other scripts should be added below

"""

config_tmpl_resolv = """
### Autogenerated by host_name.py ###
{% for ns in nameserver -%}
nameserver {{ ns }}
{% endfor -%}

{%- if domain_name %}
domain {{ domain_name }}
{%- endif %}

{%- if domain_search %}
search {{ domain_search | join(" ") }}
{%- endif %}

"""

# borrowed from: https://github.com/donjajo/py-world/blob/master/resolvconfReader.py, THX!
def get_resolvers(file):
  resolvers = []
  try:
    with open(file, 'r') as resolvconf:
      for line in resolvconf.readlines():
        line = line.split('#',1)[0];
        line = line.rstrip();
        if 'nameserver' in line:
          resolvers.append(line.split()[1])
      return resolvers
  except IOError:
    return []

default_config_data = {
    'hostname': 'vyos',
    'domain_name': '',
    'domain_search': [],
    'nameserver': [],
    'no_dhcp_ns': False
}

def get_config():
  conf = Config()
  hosts = copy.deepcopy(default_config_data)

  hosts['hostname'] = conf.return_value("system host-name")
  hosts['domain_name'] = conf.return_value("system domain-name")

  if hosts['domain_name']:
    hosts['domain_search'].append(hosts['domain_name'])

  for search in conf.return_values("system domain-search domain"):
    hosts['domain_search'].append(search)

  hosts['nameserver'] = conf.return_values("system name-server")
  hosts['no_dhcp_ns'] = conf.exists('system disable-dhcp-nameservers')

  ## system static-host-mapping
  hosts['static_host_mapping'] = { 'hostnames'  : {}}

  if conf.exists('system static-host-mapping host-name'):
    for hn in conf.list_nodes('system static-host-mapping host-name'):
      hosts['static_host_mapping']['hostnames'][hn] = {
        'ipaddr'  : conf.return_value('system static-host-mapping host-name ' + hn + ' inet'),
        'alias'   : '' 
      }
      
      if conf.exists('system static-host-mapping host-name ' + hn + ' alias'):
        a = conf.return_values('system static-host-mapping host-name ' + hn + ' alias')
        hosts['static_host_mapping']['hostnames'][hn]['alias'] = " ".join( conf.return_values('system static-host-mapping host-name ' + hn + ' alias') )

  return hosts

def verify(config):
  if config is None:
    return None

  # pattern $VAR(@) "^[[:alnum:]][-.[:alnum:]]*[[:alnum:]]$" ; "invalid host name $VAR(@)"
  hostname_regex = re.compile("^[A-Za-z0-9][-.A-Za-z0-9]*[A-Za-z0-9]$")
  if not hostname_regex.match(config['hostname']):
    raise ConfigError('Invalid host name ' + config["hostname"])

  # pattern $VAR(@) "^.{1,63}$" ; "invalid host-name length"
  length = len(config['hostname'])
  if length < 1 or length > 63:
    raise ConfigError('Invalid host-name length, must be less than 63 characters')

  # The search list is currently limited to six domains with a total of 256 characters.
  # https://linux.die.net/man/5/resolv.conf
  if len(config['domain_search']) > 6:
    raise ConfigError('The search list is currently limited to six domains')

  tmp = ' '.join(config['domain_search'])
  if len(tmp) > 256:
    raise ConfigError('The search list is currently limited to 256 characters')

  # static mappings alias hostname
  if config['static_host_mapping']['hostnames']:
    for hn in config['static_host_mapping']['hostnames']:
      for hn_alias in config['static_host_mapping']['hostnames'][hn]['alias'].split(' '):
        if not hostname_regex.match(hn_alias) and len (hn_alias) !=0:
          raise ConfigError('Invalid hostname alias ' + hn_alias)

  return None

def generate(config):
  if config is None:
    return None

  # If "system disable-dhcp-nameservers" is __configured__ all DNS resolvers
  # received via dhclient should not be added into the final 'resolv.conf'.
  #
  # We iterate over every resolver file and retrieve the received nameservers
  # for later adjustment of the system nameservers
  dhcp_ns = []
  for file in glob.glob('/etc/resolv.conf.dhclient-new*'):
    for r in get_resolvers(file):
      dhcp_ns.append(r)

  if not config['no_dhcp_ns']:
    config['nameserver'] += dhcp_ns

  # We have third party scripts altering /etc/hosts, too.
  # One example are the DHCP hostname update scripts thus we need to cache in
  # every modification first - so changing domain-name, domain-search or hostname
  # during runtime works
  old_hosts = ""
  with open(config_file_hosts, 'r') as f:
    # Skips text before the beginning of our marker.
    # NOTE: Marker __MUST__ match the one specified in config_tmpl_hosts
    for line in f:
      if line.strip() == '### modifications from other scripts should be added below':
        break

    for line in f:
      # This additional line.strip() filters empty lines
      if line.strip():
        old_hosts += line

  # Add an additional newline
  old_hosts += '\n'

  tmpl = jinja2.Template(config_tmpl_hosts)
  config_text = tmpl.render(config)

  with open(config_file_hosts, 'w') as f:
    f.write(config_text)
    f.write(old_hosts)

  tmpl = jinja2.Template(config_tmpl_resolv)
  config_text = tmpl.render(config)
  with open(config_file_resolv, 'w') as f:
    f.write(config_text)

  return None

def apply(config):
  if config is None:
    return None

  fqdn = config['hostname']
  if config['domain_name']:
    fqdn += '.' + config['domain_name']

  os.system("hostnamectl set-hostname --static {0}".format(fqdn))

  # Restart services that use the hostname
  os.system("systemctl restart rsyslog.service")

  # If SNMP is running, restart it too
  if os.system("pgrep snmpd > /dev/null") == 0:
    os.system("systemctl restart snmpd.service")
  
  # restart pdns if it is used
  if os.system("/usr/bin/rec_control ping >/dev/null") == 0:
    os.system("/etc/init.d/pdns-recursor restart >/dev/null")

  return None

if __name__ == '__main__':
  try:
    c = get_config()
    verify(c)
    generate(c)
    apply(c)
  except ConfigError as e:
    print(e)
    sys.exit(1)
