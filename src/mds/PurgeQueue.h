// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:t -*-
// vim: ts=8 sw=2 smarttab
/*
 * Ceph - scalable distributed file system
 *
 * Copyright (C) 2015 Red Hat
 *
 * This is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License version 2.1, as published by the Free Software 
 * Foundation.  See file COPYING.
 * 
 */

#ifndef PURGE_QUEUE_H_
#define PURGE_QUEUE_H_

#include "include/compact_set.h"
#include "osdc/Journaler.h"


class PurgeItem
{
public:
  inodeno_t ino;
  uint64_t size;
  file_layout_t layout;
  compact_set<int64_t> old_pools;
  SnapContext snapc;

  PurgeItem()
   : ino(0), size(0)
  {}

  void encode(bufferlist &bl) const;
  void decode(bufferlist::iterator &p);
};
WRITE_CLASS_ENCODER(PurgeItem)

/**
 * Note that this class does not take a reference to MDSRank: we are
 * independent of all the metadata structures and do not need to
 * take mds_lock for anything.
 */
class PurgeQueue
{
protected:
  CephContext *cct;
  const mds_rank_t rank;
  Mutex lock;

  int64_t metadata_pool;

  // Don't use the MDSDaemon's Finisher and Timer, because this class
  // operates outside of MDSDaemon::mds_lock
  Finisher finisher;
  SafeTimer timer;
  Filer filer;
  Objecter *objecter;
  Journaler journaler;

  std::map<uint64_t, PurgeItem> in_flight;

  //PerfCounters *logger;

  bool can_consume();

  void _consume();

  void _execute_item(
      const PurgeItem &item,
      uint64_t expire_to);
  void execute_item_complete(
      uint64_t expire_to);

public:
  void init();
  void shutdown();

  // Write an empty queue, use this during MDS rank creation
  void create(Context *completion);

  // Read the Journaler header for an existing queue and start consuming
  void open(Context *completion);

  // Submit one entry to the work queue.  Call back when it is persisted
  // to the queue (there is no callback for when it is executed)
  void push(const PurgeItem &pi, Context *completion);

  PurgeQueue(
      CephContext *cct_,
      mds_rank_t rank_,
      const int64_t metadata_pool_,
      Objecter *objecter_);
  ~PurgeQueue()
  {}
};


#endif

