#!/usr/bin/env python

# tile-generator
#
# Copyright (c) 2015-Present Pivotal Software, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import yaml
import json
import requests
import time
from urlparse import urlparse
import subprocess
import pty
import os
import select

requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)

# This function assumes that we are executing from within a concourse
# pool-resource repository, where the claimed PCF instance metadata
# is available in a file named './metadata'
#
def get_credentials():
	try:
		with open('metadata') as cred_file:
			creds = yaml.safe_load(cred_file)
			creds['opsmgr']
			creds['opsmgr']['url']
			creds['opsmgr']['username']
			creds['opsmgr']['password']
	except KeyError as e:
		print >> sys.stderr, 'metadata file is missing a value:', e.message
		sys.exit(1)
	except IOError as e:
		print >> sys.stderr, 'Not a Concourse PCF pool resource.'
		print >> sys.stderr, 'Execute this from within the pool repository root, after a successful claim/acquire.'
		sys.exit(1)
	return creds

class auth(requests.auth.AuthBase):

	def __init__(self, creds):
		self.creds = creds

	def __call__(self, request):
		url = self.creds.get('opsmgr').get('url') + '/uaa/oauth/token'
		username = self.creds.get('opsmgr').get('username')
		password = self.creds.get('opsmgr').get('password')
		headers = { 'Accept': 'application/json' }
		data = {
			'grant_type': 'password',
			'client_id': 'opsman',
			'client_secret': '',
			'username': username,
			'password': password,
			'response_type': 'token',
		}
		response = requests.post(url, data=data, verify=False, headers=headers)
		if response.status_code != requests.codes.ok:
			return requests.auth.HTTPBasicAuth(username, password)(request)
		response = response.json()
		access_token = response.get('access_token')
		token_type = response.get('token_type')
		request.headers['Authorization'] = token_type + ' ' + access_token
		return request

def get(url, stream=False, check=True):
	creds = get_credentials()
	url = creds.get('opsmgr').get('url') + url
	headers = { 'Accept': 'application/json' }
	response = requests.get(url, auth=auth(creds), verify=False, headers=headers, stream=stream)
	check_response(response, check=check)
	return response

def put(url, payload):
	creds = get_credentials()
	url = creds.get('opsmgr').get('url') + url
	response = requests.put(url, auth=auth(creds), verify=False, data=payload)
	check_response(response)
	return response

def post(url, payload):
	creds = get_credentials()
	url = creds.get('opsmgr').get('url') + url
	response = requests.post(url, auth=auth(creds), verify=False, data=payload)
	check_response(response)
	return response

def post_yaml(url, filename, payload):
	creds = get_credentials()
	url = creds.get('opsmgr').get('url') + url
	files = { filename: yaml.safe_dump(payload) }
	response = requests.post(url, auth=auth(creds), verify=False, files=files)
	check_response(response)
	return response

def upload(url, filename):
	creds = get_credentials()
	url = creds.get('opsmgr').get('url') + url
	files = { 'product[file]': open(filename, 'rb') }
	response = requests.post(url, auth=auth(creds), verify=False, files=files)
	check_response(response)
	return response

def delete(url, check=True):
	creds = get_credentials()
	url = creds.get('opsmgr').get('url') + url
	response = requests.delete(url, auth=auth(creds), verify=False)
	check_response(response, check=check)
	return response

def check_response(response, check=True):
	if check and response.status_code != requests.codes.ok:
		print >> sys.stderr, '-', response.status_code
		try:
			errors = response.json()["errors"]
			print >> sys.stderr, '- '+('\n- '.join(errors))
		except:
			print >> sys.stderr, response.text
		sys.exit(1)

def ssh():
	creds = get_credentials()
	url = creds.get('opsmgr').get('url')
	host = urlparse(url).hostname
	command = [
		'ssh',
		'-q',
		'-o', 'UserKnownHostsFile=/dev/null',
		'-o', 'StrictHostKeyChecking=no',
		'ubuntu@' + host
	]
	pid, tty = pty.fork()
	if pid == 0:
		try:
			subprocess.check_call(command)
			sys.exit(0)
		except subprocess.CalledProcessError as error:
			print 'Command failed with exit code', error.returncode
			print error.output
			sys.exit(error.returncode)
	else:
		output = os.read(tty, 4096)
		sys.stdout.write(output)
		sys.stdout.flush()
		os.write(tty, creds.get('opsmgr').get('password') + '\n')
		os.write(tty, 'stty -echo\n')
		while True:
			rlist, wlist, xlist = select.select([sys.stdin.fileno(), tty], [], [])
			if sys.stdin.fileno() in rlist:
				input = os.read(sys.stdin.fileno(), 1024)
				if len(input) == 0:
					os.close(tty)
					break
				else:
					os.write(tty, input)
					os.fsync(tty)
			elif tty in rlist:
				output = os.read(tty, 1024)
				if len(output) == 0:
					break
				sys.stdout.write(output)
				sys.stdout.flush()

def get_products():
	available_products = get('/api/products').json()
	installed_products = get('/api/installation_settings').json()['products']
	for product in available_products:
		installed = [ p for p in installed_products if p['identifier'] == product['name'] and p['product_version'] == product['product_version'] ]
		product['installed'] = len(installed) > 0
		if product['installed']:
			product['guid'] = installed[0]['guid']
	return available_products

def flatten(properties):
	additions = {}
	for key1, value1 in properties.iteritems():
		if type(value1) is dict:
			for key2, value2 in value1.iteritems():
				additions[key1 + '_' + key2] = value2
	properties.update(additions)

def configure(settings, product, properties, strict=False):
	properties = properties if properties is not None else {}
	#
	# Use the first availability zone
	#
	infrastructure = settings['infrastructure']
	product_settings = [ p for p in settings['products'] if p['identifier'] == product ]
	if len(product_settings) < 1:
		print >> sys.stderr, 'Product', product, 'does not appear to be installed'
		sys.exit(1)
	product_settings = product_settings[0]
	product_settings['availability_zone_references'] = [ az['guid'] for az in infrastructure['availability_zones'] ]
	product_settings['singleton_availability_zone_reference'] = infrastructure['availability_zones'][0]['guid']
	#
	# Make sure Elastic Runtime tile is installed
	#
	cf = [ p for p in settings['products'] if p['identifier'] == 'cf' ]
	if len(cf) < 1:
		print >> sys.stderr, 'Required product Elastic Runtime is missing'
		sys.exit(1)
	#
	# Use the Elastic Runtime stemcell
	#
	if not strict:
		stemcell = cf[0]['stemcell']
		print '- Using stemcell', stemcell['name'], 'version', stemcell['version']
		product_settings['stemcell'] = stemcell
	#
	# Insert supplied properties
	#
	missing_properties = []
	flatten(properties)
	for p in product_settings['properties']:
		key = p['identifier']
		value = properties.get(key, None)
		if value is not None:
			p['value'] = value
		else:
			if p.get('value', None) is None:
				missing_properties += [ key ]
	if len(missing_properties) > 0:
		print >> sys.stderr, 'Input file is missing required properties:'
		print >> sys.stderr, '- ' + '\n- '.join(missing_properties)
		sys.exit(1)

def get_changes(deploy_errands = None, delete_errands = None):
	if deploy_errands is None:
		deploy_errands = ['deploy-all']
	if delete_errands is None:
		delete_errands = ['delete-all']

	pending_changes_response = get('/api/v0/staged/pending_changes', check=False)
	if pending_changes_response.status_code == requests.codes.ok:
		return build_changes_1_8(deploy_errands, delete_errands, pending_changes_response)
	elif pending_changes_response.status_code == requests.codes.not_found:
		return build_changes_1_7(deploy_errands, delete_errands)
	else:
		raise Exception(
			"Unexpected response code from /api/v0/staged/pending_changes",
			pending_changes_response.status_code
		)

def build_changes_1_8(deploy_errands, delete_errands, pending_changes_response):
	changes = pending_changes_response.json()
	for product_change in changes['product_changes']:
		if product_change['action'] in ['install', 'update']:
			product_change['errands'] = [
				e for e in product_change['errands']
				if e['name'] in deploy_errands
			]
	for product_change in changes['product_changes']:
		if product_change['action'] == 'delete':
			product_change['errands'] = [
				e for e in product_change['errands']
				if e['name'] in delete_errands
			]
	return changes

def build_changes_1_7(deploy_errands, delete_errands):
	deployed = [p for p in get('/api/v0/deployed/products').json()]
	staged = [p for p in get('/api/v0/staged/products').json()]
	install = [p for p in staged if p["guid"] not in [g["guid"] for g in deployed]]
	delete = [p for p in deployed if p["guid"] not in [g["guid"] for g in staged]]
	update = [p for p in deployed if p["guid"] in [g["guid"] for g in staged]]
	for p in install + update:
		manifest = get('/api/v0/staged/products/' + p['guid'] + '/manifest').json()['manifest']
		errands = [j['name'] for j in manifest['jobs'] if j['lifecycle'] == 'errand']
		p['errands'] = []
		for deploy_errand in deploy_errands:
			if deploy_errand in errands:
				p['errands'].append({'name': deploy_errand, 'post_deploy': True})
	for p in delete:
		p['errands'] = []
		for delete_errand in delete_errands:
			p['errands'].append({'name': delete_errand, 'pre_delete': True})
	changes = {'product_changes': [{
			'guid': p['guid'],
			'errands': p.get('errands', []),
			'action': 'install' if p in install else 'delete' if p in delete else 'update'
		}
		for p in install + delete + update
	]}
	return changes

def get_cfinfo():
	settings = get('/api/installation_settings').json()
	settings = [ p for p in settings['products'] if p['identifier'] == 'cf' ]
	if len(settings) < 1:
		print >> sys.stderr, 'Elastic Runtime is not installed'
		sys.exit(1)
	settings = settings[0]
	jobs = settings['jobs']
	cc_properties = [ j for j in jobs if j['identifier'] == 'cloud_controller' ][0]['properties']
	system_domain = [ p for p in cc_properties if p['identifier'] == 'system_domain' ][0]['value']
	apps_domain = [ p for p in cc_properties if p['identifier'] == 'apps_domain' ][0]['value']
	uaa_properties = [ j for j in jobs if j['identifier'] == 'uaa' ][0]['properties']
	admin_credentials = [ c for c in uaa_properties if c['identifier'] == 'admin_credentials' ][0]['value']
	system_services_credentials = [ c for c in uaa_properties if c['identifier'] == 'system_services_credentials' ][0]['value']
	return {
		'system_domain': system_domain,
		'apps_domain': apps_domain,
		'admin_username': admin_credentials['identity'],
		'admin_password': admin_credentials['password'],
		'system_services_username': system_services_credentials['identity'],
		'system_services_password': system_services_credentials['password'],
	}

def logs(install_id):
	if install_id is None:
		install_id = last_install()
		if install_id == 0:
			print >> sys.stderr, 'No installation has ever been performed'
			sys.exit(1)
	lines_shown = 0
	running = True
	while running:
		install_status = get('/api/installation/' + str(install_id)).json()['status']
		running = install_status == 'running'
		log_lines = get('/api/installation/' + str(install_id) + '/logs').json()['logs'].splitlines()
		for line in log_lines[lines_shown:]:
			if not line.startswith('{'):
				print ' ', line
		lines_shown = len(log_lines)
		if running:
			time.sleep(1)
	if not install_status.startswith('succ'):
		print >> sys.stderr, '- install finished with status:', install_status
		sys.exit(1)

def install_exists(id):
	response = get('/api/installation/' + str(id), check=False)
	return response.status_code == requests.codes.ok

def last_install(lower=0, upper=1, check=install_exists):
	if lower == upper:
		return lower
	if check(upper):
		return last_install(upper, upper * 2, check=check)
	middle = (lower + upper + 1) / 2
	if check(middle):
		return last_install(middle, upper, check=check)
	else:
		return last_install(lower, middle - 1, check=check)
