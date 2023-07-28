#!/usr/bin/env python
import sys
import os
sys.path.append(os.path.normpath(os.path.join(os.path.dirname(__file__), '../lib')))
import init
import config
import misc
from syscoind import SyscoinDaemon
from models import Superblock, Proposal, GovernanceObject
from models import VoteSignals, VoteOutcomes, Transient
import socket
from misc import printdbg
import time
import datetime
from bitcoinrpc.authproxy import JSONRPCException
import signal
import atexit
import random
from scheduler import Scheduler
import argparse
from aiohttp import web


std_print = print

def print(*args, **kwargs):
    std_print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), *args, **kwargs)


# sync syscoind gobject list with our local relational DB backend
def perform_syscoind_object_sync(syscoind):
    GovernanceObject.sync(syscoind)


def prune_expired_proposals(syscoind):
    # vote delete for old proposals
    for proposal in Proposal.expired(syscoind.superblockcycle()):
        proposal.vote(syscoind, VoteSignals.delete, VoteOutcomes.yes)


def attempt_superblock_creation(syscoind):
    import syscoinlib

    if not syscoind.is_masternode():
        print("We are not a Masternode... can't submit superblocks!")
        return

    # query votes for this specific ebh... if we have voted for this specific
    # ebh, then it's voted on. since we track votes this is all done using joins
    # against the votes table
    #
    # has this masternode voted on *any* superblocks at the given event_block_height?
    # have we voted FUNDING=YES for a superblock for this specific event_block_height?

    event_block_height = syscoind.next_superblock_height()

    if Superblock.is_voted_funding(event_block_height):
        # printdbg("ALREADY VOTED! 'til next time!")

        # vote down any new SBs because we've already chosen a winner
        for sb in Superblock.at_height(event_block_height):
            if not sb.voted_on(signal=VoteSignals.funding):
                sb.vote(syscoind, VoteSignals.funding, VoteOutcomes.no)

        # now return, we're done
        return

    if not syscoind.is_govobj_maturity_phase():
        printdbg("Not in maturity phase yet -- will not attempt Superblock")
        return

    proposals = Proposal.approved_and_ranked(proposal_quorum=syscoind.governance_quorum(), next_superblock_max_budget=syscoind.next_superblock_max_budget())
    budget_max = syscoind.get_superblock_budget_allocation(event_block_height)
    sb_epoch_time = syscoind.block_height_to_epoch(event_block_height)

    sb = syscoinlib.create_superblock(proposals, event_block_height, budget_max, sb_epoch_time)
    if not sb:
        printdbg("No superblock created, sorry. Returning.")
        return

    # find the deterministic SB w/highest object_hash in the DB
    dbrec = Superblock.find_highest_deterministic(sb.hex_hash())
    if dbrec:
        dbrec.vote(syscoind, VoteSignals.funding, VoteOutcomes.yes)

        # any other blocks which match the sb_hash are duplicates, delete them
        for sb in Superblock.select().where(Superblock.sb_hash == sb.hex_hash()):
            if not sb.voted_on(signal=VoteSignals.funding):
                sb.vote(syscoind, VoteSignals.delete, VoteOutcomes.yes)

        printdbg("VOTED FUNDING FOR SB! We're done here 'til next superblock cycle.")
        return
    else:
        printdbg("The correct superblock wasn't found on the network...")

    # if we are the elected masternode...
    if (syscoind.we_are_the_winner()):
        printdbg("we are the winner! Submit SB to network")
        sb.submit(syscoind)

def attempt_poda_submission(syscoind):
    import config
    if config.poda_db_account_id == '':
        printdbg("PoDA DB Account ID not set.")
        return
    if config.poda_db_key_id == '':
        printdbg("PoDA DB Key ID not set.")
        return
    if config.poda_db_access_key == '':
        printdbg("PoDA DB Access Key not set.")
        return
    try:
        # fill PoDA by processing all blocks missing
        config.poda_payload.send_blobs(syscoind)
    except JSONRPCException as e:
        print("Unable to send PoDA: %s" % e.message)

def check_object_validity(syscoind):
    # vote (in)valid objects
    for gov_class in [Proposal, Superblock]:
        for obj in gov_class.select():
            obj.vote_validity(syscoind)


def is_syscoind_port_open(syscoind):
    # test socket open before beginning, display instructive message to MN
    # operators if it's not
    port_open = False
    try:
        info = syscoind.rpc_command('getgovernanceinfo')
        port_open = True
    except (socket.error, JSONRPCException) as e:
        print("%s" % e)

    return port_open

async def handle_vh(request):
    vh = request.match_info.get('vh')
    return web.Response(text=config.poda_payload.get_data(vh))

async def handle_lastblock(request):
    return web.Response(text=config.poda_payload.get_last_block())

def poda_server_loop():
    app = web.Application()
    app.add_routes([web.get('/vh/{vh}', handle_vh), web.get('/lastblock', handle_lastblock)])
    web.run_app(app)

def main():
    try:
        syscoind = SyscoinDaemon.from_syscoin_conf(config.syscoin_conf)
    except FileNotFoundError:
        syscoind = SyscoinDaemon()
        defport = 8370 if (config.network == 'mainnet') else 18370
        credList = list(syscoind.creds)
        credList[3] = int(defport)
        syscoind.creds = tuple(credList)
        pass
    options = process_args()

    # print version and return if "--version" is an argument
    if options.version:
        print("Syscoin Sentinel v%s" % config.sentinel_version)
        return
    if options.server:
        poda_server_loop()
        return
    # check syscoind connectivity
    if not is_syscoind_port_open(syscoind):
        print("Cannot connect to syscoind. Please ensure syscoind is running and the JSONRPC port is open to Sentinel.")
        return

    # check syscoind sync
    if not syscoind.is_synced():
        print("syscoind not synced with network! Awaiting full sync before running Sentinel.")
        return

    # register a handler if SENTINEL_DEBUG is set
    if os.environ.get('SENTINEL_DEBUG', None):
        import logging
        logger = logging.getLogger('peewee')
        logger.setLevel(logging.DEBUG)
        logger.addHandler(logging.StreamHandler())

    # send PoDA if configured
    attempt_poda_submission(syscoind)

    print("PoDA DB Account ID not set, using MN code path.")

    # ensure valid masternode
    if not syscoind.is_masternode():
        printdbg("Invalid Masternode Status, cannot continue.")
        return

    if options.bypass:
        # bypassing scheduler, remove the scheduled event
        printdbg("--bypass-schedule option used, clearing schedule")
        Scheduler.clear_schedule()

    if not Scheduler.is_run_time():
        printdbg("Not yet time for an object sync/vote, moving on.")
        return

    if not options.bypass:
        # delay to account for cron minute sync
        Scheduler.delay()

    # running now, so remove the scheduled event
    Scheduler.clear_schedule()

    # ========================================================================
    # general flow:
    # ========================================================================
    #
    # load "gobject list" rpc command data, sync objects into internal database
    perform_syscoind_object_sync(syscoind)

    # auto vote network objects as valid/invalid
    # check_object_validity(syscoind)

    # vote to delete expired proposals
    prune_expired_proposals(syscoind)

    # create a Superblock if necessary
    attempt_superblock_creation(syscoind)

    # schedule the next run
    Scheduler.schedule_next_run()


def signal_handler(signum, frame):
    print("Got a signal [%d], cleaning up..." % (signum))
    Transient.delete('SENTINEL_RUNNING')
    sys.exit(1)


def cleanup():
    Transient.delete(mutex_key)


def process_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-b', '--bypass-scheduler',
                        action='store_true',
                        help='Bypass scheduler and sync/vote immediately',
                        dest='bypass')
    parser.add_argument('-s', '--server',
                        action='store_true',
                        help='PoDA server',
                        dest='server')
    parser.add_argument('-v', '--version',
                        action='store_true',
                        help='Print the version (Syscoin Sentinel vX.X.X) and exit')

    args = parser.parse_args()

    return args


if __name__ == '__main__':
    atexit.register(cleanup)
    signal.signal(signal.SIGINT, signal_handler)

    # ensure another instance of Sentinel is not currently running
    mutex_key = 'SENTINEL_RUNNING'
    # assume that all processes expire after 'timeout_seconds' seconds
    timeout_seconds = 90

    is_running = Transient.get(mutex_key)
    if is_running:
        printdbg("An instance of Sentinel is already running -- aborting.")
        sys.exit(1)
    else:
        Transient.set(mutex_key, misc.now(), timeout_seconds)

    # locked to this instance -- perform main logic here
    main()

    Transient.delete(mutex_key)
