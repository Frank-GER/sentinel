from lighthouseweb3 import Lighthouse
from bitcoinrpc.authproxy import JSONRPCException
import os
import io
import sys
import syscoinlib
import boto3
import botocore
import datetime
from misc import printdbg
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'lib'))


class PoDAPayload():

    def __init__(self, token: str):
        self.connect_db(token)

    @classmethod
    def connect_db(self, token):
        # Retrieve the token from environment variable if not provided
        _token = token or os.environ.get("LIGHTHOUSE_TOKEN", "")
        if not _token:
            raise Exception(
                "No token provided: Please provide a token or set the LIGHTHOUSE_TOKEN environment variable"
            )

        self.storage_provider = Lighthouse(_token)

    @classmethod
    def get_local_block_processed(self, vh):
        import peewee
        from models import Setting
        try:
            Setting.get(Setting.name == vh)
        except (peewee.OperationalError, peewee.DoesNotExist, peewee.ProgrammingError):
            printdbg("[info]: Can't get local vh...")
            return False
        return True

    @classmethod
    def set_local_block_processed(self, blockhash):
        from models import Setting
        Setting.get_or_create(name=blockhash)

    @classmethod
    def get_last_block(self):
        import peewee
        from models import Setting
        lastblock = ''
        try:
            lastblock = Setting.get(Setting.name == 'lastpodablock').value
        except (peewee.OperationalError, peewee.DoesNotExist, peewee.ProgrammingError):
            printdbg("[info]: Can't get lastpodablock...")
        return lastblock

    @classmethod
    def set_last_block(self, lastblockIn):
        from models import Setting
        lastblock_setting, created = Setting.get_or_create(
            name='lastpodablock')
        lastblock_setting.value = lastblockIn
        lastblock_setting.save()

    @classmethod
    def send_blobs(self, syscoind):
        # get last processed block from gateway
        lastblockhash = self.get_last_block()
        # get prevCL info
        mediantimePrevCl = 0
        try:
            cl = syscoind.rpc_command('getchainlocks')
            if cl is not None:
                prevCL = cl.get('previous_chainlock')
                if prevCL is not None:
                    mediantimePrevCl = syscoind.rpc_command(
                        'getblock', prevCL.get('blockhash')).get('mediantime')
        except JSONRPCException as e:
            print("Unable to fetch prev CL: %s" % e.message)
            mediantimePrevCl = 0
        # loop through tip to last block or 7 hours back from prevCL or tip
        try:
            latestHash = syscoind.rpc_command('getbestblockhash')
            latestBlock = syscoind.rpc_command('getblock', latestHash)
            medianTimeTip = latestBlock.get('mediantime')
            mediantime = medianTimeTip
            # loop over 7 hours from tip or until lastblock_height whichever is first
            while True:
                # if prevCL - 7 hours for this block or if no prevCL then 7 hours from tip or if gateway's last block then break
                if mediantimePrevCl > 0 and (mediantimePrevCl - mediantime) > 7*60*60:
                    print(
                        "Time traversed back over 7 hours from mediantimePrevCl: %d" % mediantimePrevCl)
                    break
                elif mediantimePrevCl == 0 and (medianTimeTip - mediantime) > 7*60*60:
                    print(
                        "Time traversed back over 7 hours from medianTimeTip: %d" % medianTimeTip)
                    break
                if latestBlock.get('hash') == lastblockhash:
                    print("Found last block hash during traversal: %s" %
                          lastblockhash)
                    break
                # only process blocks that have not been processed already
                if self.get_local_block_processed(latestHash) is False:
                    # get txids and check PoDA
                    items = latestBlock.get('tx')
                    for txid in items:
                        try:
                            blobresponse = syscoind.rpc_command(
                                'getnevmblobdata', txid, True)
                            try:
                                print("checking PoDA txid {0} {1}".format(
                                    txid, self.bucketname))
                                self.get_data(blobresponse.get(
                                    'versionhash'))
                            except:
                                print("Found PoDA txid! storing in db: %s" %
                                      blobresponse.get('versionhash'))
                                current_datetime = datetime.datetime.now()
                                # send to DB backend
                                res = self.storage_provider.uploadBlob(
                                    io.BytesIO(blobresponse.get('data').encode("utf-8")), f"{current_datetime.strftime('%Y-%m-%d %H:%M')}-{blobresponse.get('versionhash')}-{txid}.txt", blobresponse.get('versionhash'))
                                if res.get('HTTPStatusCode') != 200:
                                    print('Blob Not Uploaded')
                                    return
                                pass
                            else:
                                # Something else has gone wrong.
                                print(
                                    "Unable to check for vh existance from backend: %s" % e.message)
                                raise
                        except JSONRPCException:
                            continue
                # used to check against last cached block to know when to stop processing
                latestBlock = syscoind.rpc_command(
                    'getblock', latestBlock.get('previousblockhash'))
                # need to be able to detect MTP is 7 hours old from tip to know when to stop processing
                mediantime = latestBlock.get('mediantime')
        except JSONRPCException as e:
            print("Unable to fetch latest block: %s" % e.message)
        # processed block and stored in DB so set it in cache so we can continue on from here on subsequent cycles
        self.set_last_block(latestHash)
        self.set_local_block_processed(latestHash)

    @classmethod
    def get_data(self, vh):

        tagData = self.storage_provider.getTagged(vh)
        # The object does exist.
        if (tagData.get("data") is None):
            printdbg("Data does not exist for vh: %s" % vh)
            raise
        else:
            cid = tagData.get(
                "data").get("cid")
            data, _ = self.storage_provider.download(
                cid)
            return data.decode('utf-8')
