#!/usr/bin/env python3

import logging
import logging.config
import os
import shutil

from pathlib import Path
from typing import Optional

from lookyloo.default import AbstractManager, get_config
from lookyloo.exceptions import MissingUUID, NoValidHarFile
from lookyloo.lookyloo import Lookyloo
from lookyloo.helpers import is_locked


logging.config.dictConfig(get_config('logging'))


class BackgroundIndexer(AbstractManager):

    def __init__(self, loglevel: Optional[int]=None):
        super().__init__(loglevel)
        self.lookyloo = Lookyloo()
        self.script_name = 'background_indexer'
        # make sure discarded captures dir exists
        self.discarded_captures_dir = self.lookyloo.capture_dir.parent / 'discarded_captures'
        self.discarded_captures_dir.mkdir(parents=True, exist_ok=True)

    def _to_run_forever(self):
        all_done = self._build_missing_pickles()
        if all_done:
            self._check_indexes()
        self.lookyloo.update_tree_cache_info(os.getpid(), self.script_name)

    def _build_missing_pickles(self) -> bool:
        self.logger.info('Build missing pickles...')
        # Sometimes, we have a huge backlog and the process might get stuck on old captures for a very long time
        # This value makes sure we break out of the loop and build pickles of the most recent captures
        max_captures = 50
        for uuid_path in sorted(self.lookyloo.capture_dir.glob('**/uuid'), reverse=True):
            if ((uuid_path.parent / 'tree.pickle.gz').exists() or (uuid_path.parent / 'tree.pickle').exists()):
                # We already have a pickle file
                self.logger.debug(f'{uuid_path.parent} has a pickle.')
                continue
            if not list(uuid_path.parent.rglob('*.har.gz')) and not list(uuid_path.parent.rglob('*.har')):
                # No HAR file
                self.logger.debug(f'{uuid_path.parent} has no HAR file.')
                continue

            if is_locked(uuid_path.parent):
                # it is really locked
                self.logger.debug(f'{uuid_path.parent} is locked, pickle generated by another process.')
                continue

            max_captures -= 1
            with uuid_path.open() as f:
                uuid = f.read()

            if not self.lookyloo.redis.hexists('lookup_dirs', uuid):
                # The capture with this UUID exists, but it is for some reason missing in lookup_dirs
                self.lookyloo.redis.hset('lookup_dirs', uuid, str(uuid_path.parent))
            else:
                cached_path = Path(self.lookyloo.redis.hget('lookup_dirs', uuid))
                if cached_path != uuid_path.parent:
                    # we have a duplicate UUID, it is proably related to some bad copy/paste
                    if cached_path.exists():
                        # Both paths exist, move the one that isn't in lookup_dirs
                        self.logger.critical(f'Duplicate UUID for {uuid} in {cached_path} and {uuid_path.parent}, discarding the latest')
                        shutil.move(str(uuid_path.parent), str(self.discarded_captures_dir / uuid_path.parent.name))
                        continue
                    else:
                        # The path in lookup_dirs for that UUID doesn't exists, just update it.
                        self.lookyloo.redis.hset('lookup_dirs', uuid, str(uuid_path.parent))

            try:
                self.logger.info(f'Build pickle for {uuid}: {uuid_path.parent.name}')
                self.lookyloo.get_crawled_tree(uuid)
                self.lookyloo.trigger_modules(uuid, auto_trigger=True)
                self.logger.info(f'Pickle for {uuid} build.')
            except MissingUUID:
                self.logger.warning(f'Unable to find {uuid}. That should not happen.')
            except NoValidHarFile as e:
                self.logger.critical(f'There are no HAR files in the capture {uuid}: {uuid_path.parent.name} - {e}')
            except Exception as e:
                self.logger.critical(f'Unable to build pickle for {uuid}: {uuid_path.parent.name} - {e}')
                # The capture is not working, moving it away.
                self.lookyloo.redis.hdel('lookup_dirs', uuid)
                shutil.move(str(uuid_path.parent), str(self.discarded_captures_dir / uuid_path.parent.name))
            if max_captures <= 0:
                break
        else:
            self.logger.info('... done.')
            return True
        self.logger.info('... too many captures in the backlog, start from the beginning.')
        return False

    def _check_indexes(self):
        index_redis = self.lookyloo.indexing.redis
        can_index = index_redis.set('ongoing_indexing', 1, ex=300, nx=True)
        if not can_index:
            # There is no reason to run this method in multiple scripts.
            self.logger.info('Indexing already ongoing in another process.')
            return
        self.logger.info('Check indexes...')
        for cache in self.lookyloo.sorted_capture_cache(cached_captures_only=False):
            if self.lookyloo.is_public_instance and cache.no_index:
                # Capture unindexed
                continue
            p = index_redis.pipeline()
            p.sismember('indexed_urls', cache.uuid)
            p.sismember('indexed_body_hashes', cache.uuid)
            p.sismember('indexed_cookies', cache.uuid)
            p.sismember('indexed_hhhashes', cache.uuid)
            indexed = p.execute()
            if all(indexed):
                continue
            try:
                ct = self.lookyloo.get_crawled_tree(cache.uuid)
            except NoValidHarFile:
                self.logger.warning(f'Broken pickle for {cache.uuid}')
                self.lookyloo.remove_pickle(cache.uuid)
                continue

            if not indexed[0]:
                self.logger.info(f'Indexing urls for {cache.uuid}')
                self.lookyloo.indexing.index_url_capture(ct)
            if not indexed[1]:
                self.logger.info(f'Indexing resources for {cache.uuid}')
                self.lookyloo.indexing.index_body_hashes_capture(ct)
            if not indexed[2]:
                self.logger.info(f'Indexing cookies for {cache.uuid}')
                self.lookyloo.indexing.index_cookies_capture(ct)
            if not indexed[3]:
                self.logger.info(f'Indexing HH Hashes for {cache.uuid}')
                self.lookyloo.indexing.index_http_headers_hashes_capture(ct)
            # NOTE: categories aren't taken in account here, should be fixed(?)
            # see indexing.index_categories_capture(capture_uuid, categories)
        index_redis.delete('ongoing_indexing')
        self.logger.info('... done.')


def main():
    i = BackgroundIndexer()
    i.run(sleep_in_sec=60)


if __name__ == '__main__':
    main()
