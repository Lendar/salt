import json
import logging

import salt.netapi

logger = logging.getLogger(__name__)


class SaltInfo:
    '''
    Class to  handle processing and publishing of "real time" Salt upates.
    '''

    def __init__(self, handler):
        '''
        handler is expected to be the server side end of a websocket
        connection.
        '''
        self.handler = handler

        '''
        These represent a "real time" view into Salt's jobs.
        '''
        self.jobs = {}

        '''
        This represents a "real time" view of minions connected to
        Salt.
        '''
        self.minions = {}

    def publish_minions(self):
        '''
        Publishes minions as a list of dicts.
        '''
        logger.debug('in publish minions')
        minions = {}

        logger.debug('starting loop')
        for minion, minion_info in self.minions.iteritems():
            logger.debug(minion)
            # logger.debug(minion_info)
            curr_minion = {}
            curr_minion.update(minion_info)
            curr_minion.update({'id': minion})
            minions[minion] = curr_minion
        logger.debug('ended loop')
        ret = {'minions': minions}
        self.handler.write_message(u'{}\n\n'.format(json.dumps(ret)))

    def publish(self, key, data):
        '''
        Publishes the data to the event stream.
        '''
        publish_data = {key: data}
        pub = u'{}\n\n'.format(json.dumps(publish_data))
        self.handler.write_message(pub)

    def process_minion_update(self, event_data):
        '''
        Associate grains data with a minion and publish minion update
        '''
        tag = event_data['tag']
        event_info = event_data['data']

        _, _, _, _, mid = tag.split('/')

        if not self.minions.get(mid, None):
            self.minions[mid] = {}

        minion = self.minions[mid]

        minion.update({'grains': event_info['return']})
        logger.debug("In process minion grains update with minions={}".format(self.minions.keys()))
        self.publish_minions()

    def process_ret_job_event(self, event_data):
        '''
        Process a /ret event returned by Salt for a particular minion.
        These events contain the returned results from a particular execution.
        '''
        tag = event_data['tag']
        event_info = event_data['data']

        _, _, jid, _, mid = tag.split('/')
        job = self.jobs.setdefault(jid, {})

        minion = job.setdefault('minions', {}).setdefault(mid, {})
        minion.update({'return': event_info['return']})
        minion.update({'retcode': event_info['retcode']})
        minion.update({'success': event_info['success']})

        job_complete = all([minion['success'] for mid, minion
                            in job['minions'].iteritems()])

        if job_complete:
            job['state'] = 'complete'

        self.publish('jobs', self.jobs)

    def process_new_job_event(self, event_data):
        '''
        Creates a new job with properties from the event data
        like jid, function, args, timestamp.

        Also sets the initial state to started.

        Minions that are participating in this job are also noted.

        '''
        job = None
        tag = event_data['tag']
        event_info = event_data['data']
        minions = {}
        for mid in event_info['minions']:
            minions[mid] = {'success': False}

        job = {
            'jid': event_info['jid'],
            'start_time': event_info['_stamp'],
            'minions': minions,  # is a dictionary keyed by mids
            'fun': event_info['fun'],
            'tgt': event_info['tgt'],
            'tgt_type': event_info['tgt_type'],
            'state': 'running',
        }
        self.jobs[event_info['jid']] = job
        self.publish('jobs', self.jobs)

    def process_key_event(self, event_data):
        tag = event_data['tag']
        event_info = event_data['data']

        '''
        Tag: salt/key
        Data:
        {'_stamp': '2014-05-20T22:45:04.345583',
         'act': 'delete',
         'id': 'compute.home',
         'result': True}
        '''

        if event_info['act'] == 'delete':
            self.minions.pop(event_info['id'], None)
        elif event_info['act'] == 'accept':
            self.minions.setdefault(event_info['id'], {})

        self.publish_minions()

    def process_presence_events(self, salt_data, token, opts):
        '''
        Check if any minions have connected or dropped.
        Send a message to the client if they have.
        '''
        logger.debug('In presence')
        changed = False

        # check if any connections were dropped
        if set(salt_data['data'].get('lost', [])):
            dropped_minions = set(salt_data['data'].get('lost', []))
        else:
            dropped_minions = set(self.minions.keys()) - set(salt_data['data'].get('present', []))

        for minion in dropped_minions:
            changed = True
            logger.debug('Popping {}'.format(minion))
            self.minions.pop(minion, None)

        # check if any new connections were made
        if set(salt_data['data'].get('new', [])):
            logger.debug('got new minions')
            new_minions = set(salt_data['data'].get('new', []))
            changed = True
        elif set(salt_data['data'].get('present', [])) - set(self.minions.keys()):
            logger.debug('detected new minions')
            new_minions = set(salt_data['data'].get('present', [])) - set(self.minions.keys())
            changed = True
        else:
            new_minions = []

        tgt = ','.join(new_minions)
        for mid in new_minions:
            logger.debug('Adding minion')
            self.minions[mid] = {}

        if tgt:
            changed = True
            client = salt.netapi.NetapiClient(opts)
            client.run(
                {
                    'fun': 'grains.items',
                    'tgt': tgt,
                    'expr_type': 'list',
                    'mode': 'client',
                    'client': 'local',
                    'async': 'local_async',
                    'token': token,
                })

        if changed:
            self.publish_minions()

    def process(self, salt_data, token, opts):
        '''
        Process events and publish data
        '''
        import threading
        logger.debug('In process {}'.format(threading.current_thread()))
        logger.debug(salt_data['tag'])
        logger.debug(salt_data)

        parts = salt_data['tag'].split('/')
        if len(parts) < 2:
            return

        # TBD: Simplify these conditional expressions
        if parts[1] == 'job':
            logger.debug('In job part 1')
            if parts[3] == 'new':
                logger.debug('In new job')
                self.process_new_job_event(salt_data)
                # if salt_data['data']['fun'] == 'grains.items':
                #     self.minions = {}
            elif parts[3] == 'ret':
                logger.debug('In ret')
                self.process_ret_job_event(salt_data)
                if salt_data['data']['fun'] == 'grains.items':
                    self.process_minion_update(salt_data)
        elif parts[1] == 'key':
            logger.debug('In key')
            self.process_key_event(salt_data)
        elif parts[1] == 'presence':
            self.process_presence_events(salt_data, token, opts)
            # logger.debug('In presence')
