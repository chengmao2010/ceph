
"""
Demonstrate writing a Ceph web interface inside a mgr module.
"""

# We must share a global reference to this instance, because it is the
# gatekeeper to all accesses to data from the C++ side (e.g. the REST API
# request handlers need to see it)
from collections import defaultdict
import collections

_global_instance = {'plugin': None}
def global_instance():
    assert _global_instance['plugin'] is not None
    return _global_instance['plugin']


import os
import logging
import logging.config
import json
import sys

import cherrypy
import jinja2

from mgr_module import MgrModule

from rest.app.types import OsdMap, NotFound, Config, FsMap, MonMap, \
    PgSummary, Health, MonStatus

log = logging.getLogger("guilolz")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rest.app.settings")

django_log = logging.getLogger("django.request")
django_log.addHandler(logging.StreamHandler())
django_log.setLevel(logging.DEBUG)


# How many cluster log lines shall we hold onto in our
# python module for the convenience of the GUI?
LOG_BUFFER_SIZE = 30


def recurse_refs(root, path):
    if isinstance(root, dict):
        for k, v in root.items():
            recurse_refs(v, path + "->%s" % k)
    elif isinstance(root, list):
        for n, i in enumerate(root):
            recurse_refs(i, path + "[%d]" % n)

    log.info("%s %d (%s)" % (path, sys.getrefcount(root), root.__class__))


class Module(MgrModule):
    def __init__(self, *args, **kwargs):
        super(Module, self).__init__(*args, **kwargs)
        _global_instance['plugin'] = self
        self.log.info("Constructing module {0}: instance {1}".format(
            __name__, _global_instance))

        self.log_buffer = collections.deque(maxlen=LOG_BUFFER_SIZE)

    def notify(self, notify_type, notify_val):
        if notify_type == "clog":
            log.info("clog: {0}".format(notify_val["message"]))
            log.info("clog: {0}".format(json.dumps(notify_val)))
            self.log_buffer.appendleft(notify_val)
        else:
            pass

    def get_sync_object(self, object_type, path=None):
        if object_type == OsdMap:
            data = self.get("osd_map")

            assert data is not None

            data['tree'] = self.get("osd_map_tree")
            data['crush'] = self.get("osd_map_crush")
            data['crush_map_text'] = self.get("osd_map_crush_map_text")
            data['osd_metadata'] = self.get("osd_metadata")
            obj = OsdMap(data['epoch'], data)
        elif object_type == Config:
            data = self.get("config")
            obj = Config(0, data)
        elif object_type == MonMap:
            data = self.get("mon_map")
            obj = MonMap(data['epoch'], data)
        elif object_type == FsMap:
            data = self.get("fs_map")
            obj = FsMap(data['epoch'], data)
        elif object_type == PgSummary:
            data = self.get("pg_summary")
            self.log.debug("JSON: {0}".format(data))
            obj = PgSummary(0, data)
        elif object_type == Health:
            data = self.get("health")
            obj = Health(0, json.loads(data['json']))
        elif object_type == MonStatus:
            data = self.get("mon_status")
            obj = MonStatus(0, json.loads(data['json']))
        else:
            raise NotImplementedError(object_type)

        # TODO: move 'path' handling up into C++ land so that we only
        # Pythonize the part we're interested in
        if path:
            try:
                for part in path:
                    if isinstance(obj, dict):
                        obj = obj[part]
                    else:
                        obj = getattr(obj, part)
            except (AttributeError, KeyError):
                raise NotFound(object_type, path)

        return obj

    def shutdown(self):
        cherrypy.engine.stop()

    def get_latest(self, daemon_type, daemon_name, stat):
        data = self.get_counter(daemon_type, daemon_name, stat)[stat]
        if data:
            return data[-1][1]
        else:
            return 0

    def get_rate(self, daemon_type, daemon_name, stat):
        data = self.get_counter(daemon_type, daemon_name, stat)[stat]

        if data and len(data) > 1:
            return (data[-1][1] - data[-2][1]) / float(data[-1][0] - data[-2][0])
        else:
            return 0

    def format_dimless(self, n, width, colored=True):
        """
        Format a number without units, so as to fit into `width` characters, substituting
        an appropriate unit suffix.
        """
        units = [' ', 'k', 'M', 'G', 'T', 'P']
        unit = 0
        while len("%s" % (int(n) // (1000**unit))) > width - 1:
            unit += 1

        if unit > 0:
            truncated_float = ("%f" % (n / (1000.0 ** unit)))[0:width - 1]
            if truncated_float[-1] == '.':
                truncated_float = " " + truncated_float[0:-1]
        else:
            truncated_float = "%{wid}d".format(wid=width-1) % n
        formatted = "%s%s" % (truncated_float, units[unit])

        if colored:
            # TODO: html equivalent
            # if n == 0:
            #     color = self.BLACK, False
            # else:
            #     color = self.YELLOW, False
            # return self.bold(self.colorize(formatted[0:-1], color[0], color[1])) \
            #     + self.bold(self.colorize(formatted[-1], self.BLACK, False))
            return formatted
        else:
            return formatted

    def fs_status(self, fs_id):
        mds_versions = defaultdict(list)

        fsmap = self.get("fs_map")
        filesystem = None
        for fs in fsmap['filesystems']:
            if fs['id'] == fs_id:
                filesystem = fs
                break

        rank_table = []

        mdsmap = filesystem['mdsmap']

        client_count = 0

        for rank in mdsmap["in"]:
            up = "mds_{0}".format(rank) in mdsmap["up"]
            if up:
                gid = mdsmap['up']["mds_{0}".format(rank)]
                info = mdsmap['info']['gid_{0}'.format(gid)]
                dns = self.get_latest("mds", info['name'], "mds.inodes")
                inos = self.get_latest("mds", info['name'], "mds_mem.ino")

                if rank == 0:
                    client_count = self.get_latest("mds", info['name'],
                                                   "mds_sessions.session_count")
                elif client_count == 0:
                    # In case rank 0 was down, look at another rank's
                    # sessionmap to get an indication of clients.
                    client_count = self.get_latest("mds", info['name'],
                                                   "mds_sessions.session_count")

                laggy = "laggy_since" in info

                state = info['state'].split(":")[1]
                if laggy:
                    state += "(laggy)"

                # if state == "active" and not laggy:
                #     c_state = self.colorize(state, self.GREEN)
                # else:
                #     c_state = self.colorize(state, self.YELLOW)

                # Populate based on context of state, e.g. client
                # ops for an active daemon, replay progress, reconnect
                # progress
                activity = ""

                if state == "active":
                    activity = "Reqs: " + self.format_dimless(
                        self.get_rate("mds", info['name'], "mds_server.handle_client_request"),
                        5
                    ) + "/s"

                metadata = self.get_metadata('mds', info['name'])
                mds_versions[metadata['ceph_version']].append(info['name'])
                rank_table.append(
                    {
                        "rank": rank,
                        "state": state,
                        "mds": info['name'],
                        "activity": activity,
                        "dns": dns,
                        "inos": inos
                    }
                )

            else:
                rank_table.append(
                    {
                        "rank": rank,
                        "state": "failed",
                        "mds": "",
                        "activity": "",
                        "dns": 0,
                        "inos": 0
                    }
                )

        # Find the standby replays
        for gid_str, daemon_info in mdsmap['info'].iteritems():
            if daemon_info['state'] != "up:standby-replay":
                continue

            inos = self.get_latest("mds", daemon_info['name'], "mds_mem.ino")
            dns = self.get_latest("mds", daemon_info['name'], "mds.inodes")

            activity = "Evts: " + self.format_dimless(
                self.get_rate("mds", daemon_info['name'], "mds_log.replay"),
                5
            ) + "/s"

            rank_table.append(
                {
                    "rank": "{0}-s".format(daemon_info['rank']),
                    "state": "standby-replay",
                    "mds": daemon_info['name'],
                    "activity": activity,
                    "dns": dns,
                    "inos": inos
                }
            )

        df = self.get("df")
        pool_stats = dict([(p['id'], p['stats']) for p in df['pools']])
        osdmap = self.get("osd_map")
        pools = dict([(p['pool'], p) for p in osdmap['pools']])
        metadata_pool_id = mdsmap['metadata_pool']
        data_pool_ids = mdsmap['data_pools']

        pools_table = []
        for pool_id in [metadata_pool_id] + data_pool_ids:
            pool_type = "metadata" if pool_id == metadata_pool_id else "data"
            stats = pool_stats[pool_id]
            pools_table.append({
                "pool": pools[pool_id]['pool_name'],
                "type": pool_type,
                "used": stats['bytes_used'],
                "avail": stats['max_avail']
            })

        standby_table = []
        for standby in fsmap['standbys']:
            metadata = self.get_metadata('mds', standby['name'])
            mds_versions[metadata['ceph_version']].append(standby['name'])

            standby_table.append({
                'name': standby['name']
            })

        return {
            "filesystem": {
                "id": fs_id,
                "name": mdsmap['fs_name'],
                "client_count": client_count,
                "ranks": rank_table,
                "pools": pools_table
            },
            "standbys": standby_table,
            "versions": mds_versions
        }

    def serve(self):
        current_dir = os.path.dirname(os.path.abspath(__file__))

        jinja_loader = jinja2.FileSystemLoader(current_dir)
        env = jinja2.Environment(loader=jinja_loader)

        class Root(object):
            def _toplevel_data(self):
                """
                Data consumed by the base.html template
                """
                fsmap = global_instance().get_sync_object(FsMap)
                filesystems = [
                    {
                        "id": f['id'],
                        "name": f['mdsmap']['fs_name'],
                        "url": "/filesystem/{0}/".format(f['id'])
                    }
                    for f in fsmap.data['filesystems']
                ]

                return {
                    'health': global_instance().get_sync_object(Health).data,
                    'filesystems': filesystems
                }

            @cherrypy.expose
            def filesystem(self, fs_id):
                template = env.get_template("status.html")

                toplevel_data = self._toplevel_data()

                content_data = {
                    "fs_status": global_instance().fs_status(int(fs_id))
                }

                return template.render(
                    ceph_version=global_instance().version,
                    toplevel_data=json.dumps(toplevel_data, indent=2),
                    content_data=json.dumps(content_data, indent=2)
                )

            @cherrypy.expose
            def health(self):
                template = env.get_template("health.html")
                return template.render(
                    ceph_version=global_instance().version,
                    toplevel_data=json.dumps(self._toplevel_data(), indent=2),
                    content_data=json.dumps(self._health(), indent=2)
                )

            @cherrypy.expose
            def servers(self):
                template = env.get_template("servers.html")
                return template.render(
                    ceph_version=global_instance().version,
                    toplevel_data=json.dumps(self._toplevel_data(), indent=2),
                    content_data=json.dumps(self._servers(), indent=2)
                )

            def _servers(self):
                open("/tmp/out", 'w').write(json.dumps(global_instance().list_servers(), indent=2))
                return {
                    'servers': global_instance().list_servers()
                }

            @cherrypy.expose
            @cherrypy.tools.json_out()
            def servers_data(self):
                return self._servers()

            def _health(self):
                # Fuse osdmap with pg_summary to get description of pools
                # including their PG states
                osd_map = global_instance().get_sync_object(OsdMap).data
                pg_summary = global_instance().get_sync_object(PgSummary).data
                df = global_instance().get("df")
                pool_stats = dict([(p['id'], p['stats']) for p in df['pools']])
                pools = []

                for pool in osd_map['pools']:
                    pool['pg_status'] = pg_summary['by_pool'][pool['pool'].__str__()]
                    pool['stats'] = pool_stats[pool['pool']]
                    pools.append(pool)

                # Not needed, skip the effort of transmitting this
                # to UI
                del osd_map['pg_temp']

                return {
                    "health": global_instance().get_sync_object(Health).data,
                    "mon_status": global_instance().get_sync_object(
                        MonStatus).data,
                    "osd_map": osd_map,
                    "clog": list(global_instance().log_buffer),
                    "pools": pools
                }

            @cherrypy.expose
            @cherrypy.tools.json_out()
            def health_data(self):
                return self._health()

            @cherrypy.expose
            def index(self):
                return self.health()

            @cherrypy.expose
            @cherrypy.tools.json_out()
            def filesystem_data(self, fs_id):
                return global_instance().fs_status(int(fs_id))

            @cherrypy.expose
            @cherrypy.tools.json_out()
            def toplevel_data(self):
                return self._toplevel_data()

            def _get_mds_names(self, filesystem_id=None):
                names = []

                fsmap = global_instance().get("fs_map")
                for fs in fsmap['filesystems']:
                    if filesystem_id is not None and fs['id'] != filesystem_id:
                        continue
                    names.extend([info['name'] for _, info in fs['mdsmap']['info'].items()])

                if filesystem_id is None:
                    names.extend(info['name'] for info in fsmap['standbys'])

                return names

            @cherrypy.expose
            @cherrypy.tools.json_out()
            def mds_counters(self, fs_id):
                """
                Result format: map of daemon name to map of counter to list of datapoints
                """

                # Opinionated list of interesting performance counters for the GUI --
                # if you need something else just add it.  See how simple life is
                # when you don't have to write general purpose APIs?
                counters = [
                    "mds_server.handle_client_request",
                    "mds_log.ev",
                    "mds_cache.num_strays",
                    "mds.exported",
                    "mds.exported_inodes",
                    "mds.imported",
                    "mds.imported_inodes",
                    "mds.inodes",
                    "mds.caps",
                    "mds.subtrees"
                ]

                result = {}
                mds_names = self._get_mds_names(int(fs_id))

                for mds_name in mds_names:
                    result[mds_name] = {}
                    for counter in counters:
                        data = global_instance().get_counter("mds", mds_name, counter)
                        if data is not None:
                            result[mds_name][counter] = data[counter]
                        else:
                            result[mds_name][counter] = []

                return dict(result)

        # Configure django.request logger
        logging.getLogger("django.request").handlers = self.log.handlers
        logging.getLogger("django.request").setLevel(logging.DEBUG)

        cherrypy.config.update({
            'server.socket_port': 7000,
            'engine.autoreload.on': False
        })

        static_dir = os.path.join(current_dir, 'static')
        conf = {
            "/static": {
                "tools.staticdir.on": True,
                'tools.staticdir.dir': static_dir
            }
        }
        log.info("Serving static from {0}".format(static_dir))
        cherrypy.tree.mount(Root(), "/", conf)

        cherrypy.engine.start()
        cherrypy.engine.block()
