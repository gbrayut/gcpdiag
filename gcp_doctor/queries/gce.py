# Lint as: python3
"""Queries related to GCP Kubernetes Engine clusters."""

import functools
import logging
import re
from typing import Dict, List, Mapping, Optional

import googleapiclient.errors

from gcp_doctor import config, models, utils
from gcp_doctor.queries import apis


class Instance(models.Resource):
  """Represents a GCE instance."""
  _resource_data: dict

  def __init__(self, project_id, resource_data):
    super().__init__(project_id=project_id)
    self._resource_data = resource_data

  @property
  def name(self) -> str:
    return self._resource_data['name']

  def get_full_path(self) -> str:
    result = re.match(r'https://www.googleapis.com/compute/v1/(.*)',
                      self._resource_data['selfLink'])
    if result:
      return result.group(1)
    else:
      return '>> ' + self._resource_data['selfLink']

  def get_short_path(self) -> str:
    # Note: instance names must be unique per project, so no need to add the zone.
    path = self.project_id + '/' + self.name
    return path

  def is_gke_node(self) -> bool:
    return 'labels' in self._resource_data and \
           'goog-gke-node' in self._resource_data['labels']

  @property
  def service_account(self) -> Optional[str]:
    if 'serviceAccounts' in self._resource_data:
      saccts = self._resource_data['serviceAccounts']
      if isinstance(saccts, list) and len(saccts) >= 1:
        return saccts[0]['email']
    return None

  @property
  def service_account_scopes(self) -> Optional[List[str]]:
    if 'serviceAccounts' in self._resource_data:
      saccts = self._resource_data['serviceAccounts']
      if isinstance(saccts, list) and len(saccts) >= 1:
        return saccts[0]['scopes']
    return None


@functools.lru_cache(maxsize=None)
def get_instances(context: models.Context) -> Mapping[str, Instance]:
  """Get a list of Instance matching the given context, indexed by instance id."""
  # TODO: somehow reduce complexity
  instances: Dict[str, Instance] = {}
  pages_to_fetch = []

  def instances_list_callback(request, response, exception):
    del request
    if exception:
      logging.error('exception when listing instances: %s', exception)
      return
    if not response or 'items' not in response:
      return
    for i in response['items']:
      result = re.match(
          r'https://www.googleapis.com/compute/v1/projects/([^/]+)/zones/([^/]+)/',
          i['selfLink'])
      if not result:
        logging.error('instance %s selfLink didn\'t match regexp: %s', i['id'],
                      i['selfLink'])
        continue
      project_id = result.group(1)
      zone = result.group(2)
      labels = i.get('labels', {})
      if not context.match_project_resource(location=zone, labels=labels):
        continue
      instances[i['id']] = Instance(project_id=project_id, resource_data=i)
    if 'nextPageToken' in response and response['nextPageToken']:
      pages_to_fetch.append((zone, response['nextPageToken']))

  gce_api = apis.get_api('compute', 'v1')
  for project_id in context.projects:
    try:
      logging.info('listing gce zones of project %s', project_id)
      request = gce_api.zones().list(project=project_id)
      response = request.execute(num_retries=config.API_RETRIES)
      if not response or 'items' not in response:
        continue
      logging.info('listing gce instances of project %s', project_id)
      batch = gce_api.new_batch_http_request()
      for zone in response['items']:
        if not 'name' in zone:
          continue
        # If the context filters by regions, make sure that we query only those
        # regions.
        if context.regions:
          if not any(1 for r in context.regions if zone['name'].startswith(r)):
            continue
        batch.add(gce_api.instances().list(project=project_id,
                                           zone=zone['name']),
                  callback=instances_list_callback)
      batch.execute()

      # Continue fetching pages, until there are no more left.
      page = 1
      while pages_to_fetch:
        page += 1
        logging.info('listing gce instances of project %s (page: %d)',
                     project_id, page)
        batch = gce_api.new_batch_http_request()
        pages_to_fetch_now = pages_to_fetch
        pages_to_fetch = []
        for p in pages_to_fetch_now:
          batch.add(gce_api.instances().list(project=project_id,
                                             zone=p[0],
                                             pageToken=p[1]),
                    callback=instances_list_callback)
        batch.execute()

    except googleapiclient.errors.HttpError as err:
      errstr = utils.http_error_message(err)
      raise ValueError(
          f'can\'t list instances for project {project_id}: {errstr}') from err
  return instances