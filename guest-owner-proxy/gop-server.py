#!/usr/bin/env python3

from Crypto.Cipher import AES
from Crypto.Util import Counter
from concurrent import futures
from optparse import OptionParser
from os import path, urandom
from pathlib import Path
from uuid import UUID

from pre_attestation_pb2 import BundleResponse, SecretResponse
import pre_attestation_pb2_grpc

import grpc
import json
import subprocess
import time
import hmac
import base64
import hashlib
import logging
import os

keysets = {}
hag_path = "hag"
certs_path = "/tmp/csv-guest-owner-proxy/certs/"
ovmf_path = "/opt/csv/ccv0-guest/OVMF.fd"
cmdline_file = "/opt/csv/ccv0-guest/cmdline"
kernel_file = "/opt/csv/ccv0-guest/vmlinuz-5.15.0-rc5+"
initrd_file = "/opt/csv/ccv0-guest/initrd.pre.img"
connection_id = 0
build_id = 1600
log_level_output = logging.INFO
enable_measurement = True 

def guid_to_le(guid_str):
    return UUID("{" + guid_str + "}").bytes_le

def construct_secret_entry(guid_str, secret_bytes):
    l = 16 + 4 + len(secret_bytes)
    entry = bytearray(l);
    entry[0:16] = guid_to_le(guid_str)
    entry[16:20] = l.to_bytes(4, byteorder='little')
    entry[20:20+len(secret_bytes)] = secret_bytes
    return entry


def cli_parsing():
    usage = '''usage: %prog [options] [command]
    '''

    _parser = OptionParser(usage)

    _parser.add_option("-p", "--port", \
                       dest = "grpc_port", \
                       default = 50051, \
                       help = "Set port for gRPC server.")

    _parser.add_option("-c", "--config", \
                       dest = "config_path", \
                       default = "keysets.json", \
                       help = "Path to keyset config file.")

    _parser.add_option("-k", "--keyfile", \
                       dest = "keyfile_path", \
                       default = "keys.json", \
                       help = "Path to keyfile.")

    _parser.add_option("-l", "--logfile", \
                       dest = "logfile_path", \
                       default = "gop-server.LOG", \
                       help = "Path to log file.")

    _parser.add_option("-d", "--debug", \
                       dest = "debug", \
                       action = "store_true", \
                       help = "Log output DEBUG level")

    _parser.add_option("-u", "--unsafe", \
                       dest = "unsafe", \
                       action = "store_true", \
                       help = "Unsafe mode: measurement validation skipped")

    _parser.set_defaults()
    (_options, _args) = _parser.parse_args()

    return _options

def main(options):
    global log_level_output
    global enable_measurement
    try:
        with open(options.config_path) as f:
            global keysets
            keysets = json.load(f)
    except Exception as e:
        print("Failed to load config: {}".format(e.msg))

    # this will be replaced by an HSM probably
    try:
        with open(options.keyfile_path) as f:
            global keys
            keys = json.load(f)
    except Exception as e:
        print("Failed to load keyfile: {}".format(e.msg))

    print("Guest Owner Proxy started on port "+str(options.grpc_port))
    logging.info("Guest Owner Proxy started on port "+str(options.grpc_port))

    if options.debug: 
        log_level_output = logging.DEBUG
        print("Debug enabled")
    if options.unsafe: 
        enable_measurement = False 
        print("Measurement validation disabled")

    logging.basicConfig(filename=options.logfile_path, \
            format='%(asctime)s :: %(message)s', \
            level=log_level_output)

    # this demo implementation isn't designed for parallelism.
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    pre_attestation_pb2_grpc.add_SetupServicer_to_server(SetupService(), server)

    server.add_insecure_port("[::]:{}".format(options.grpc_port))
    server.start()
    server.wait_for_termination()


class SetupService(pre_attestation_pb2_grpc.SetupServicer):
    def __init__(self):
        self.connection_id = 1

    # TODO: error handle failed request
    def GetLaunchBundle(self, request, context):
        logging.debug("Launch Bundle Request: {}".format(request))
        cid = self.connection_id
        self.connection_id += 1

        # make dir for this conection
        connection_certs_path = path.join(certs_path, "connection{}".format(cid))
        Path(connection_certs_path).mkdir(parents=True, exist_ok=True)

        # save pdh to file
        with open(path.join(connection_certs_path,"pdh.cert"),"wb") as f:
            f.write(request.PlatformPublicKey)

        # generate launch blob
        # use hag for now. might switch in the future

        cmd = "sudo {} --set_out_dir {}". \
                format(hag_path, connection_certs_path)
        os.system(cmd)

        cmd = "sudo cp /opt/csv/pdh.cert {}". \
                format(connection_certs_path)
        os.system(cmd)

        cmd = "sudo cp {} {}". \
                format(cmdline_file, connection_certs_path)
        os.system(cmd)

        cmd = "sudo cp {} {}/initramfs.img". \
                format(initrd_file, connection_certs_path)
        os.system(cmd)

        cmd = "sudo cp {} {}/bzImage". \
                format(kernel_file, connection_certs_path)
        os.system(cmd)

        cmd = "sudo {} --generate_policy 0 0 0 0 0 0 0 0". \
                format(hag_path)
        os.system(cmd)

        os.chdir(connection_certs_path)

        cmd = "sudo hag --generate_launch_blob {} {} true". \
                format(build_id, ovmf_path)
        os.system(cmd)


        cmd = r"sudo tr -d '\n' < {}/launch_blob.bin > {}/launch_blob.b64".format(connection_certs_path, connection_certs_path)
        os.system(cmd)

        cmd = r"sudo tr -d '\n' < {}/guest_owner_dh.cert > {}/guest_owner_dh.b64".format(connection_certs_path, connection_certs_path)
        os.system(cmd)

        logging.info("Launch Bundle created for connection{}".format(cid))

        # read in the launch blob
        with open(path.join(connection_certs_path, "launch_blob.b64"), "rb") as f:
            launch_blob = f.read()

        # read in the guest owner public key
        with open(path.join(connection_certs_path, "guest_owner_dh.b64"), "rb") as f:
            godh = f.read()

        response = BundleResponse(GuestOwnerPublicKey = godh, \
                LaunchBlob = launch_blob, \
                ConnectionId = cid)

        logging.debug("Launch Bundle Response: {}".format(response))
        return response


    # TODO: make into smaller functions
    # TODO: clean up connection state after verification 
    def GetLaunchSecret(self, request, context) :
        logging.debug("Launch Secret Request: {}".format(request))

        # check each of the parameters against the keyset
        if not request.KeysetId in keysets:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details('KEYSET INVALID')
            logging.info("Launch Secret Request Failed: Bad Keyset")
            return SecretResponse()

        keyset = keysets[request.KeysetId]

        # policy
        if not request.Policy in keyset['allowed-policies']:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details('POLICY INVALID')
            logging.info("Launch Secret Request Failed: Bad Policy")
            return SecretResponse()

        # api
        if not request.ApiMajor >= keyset['min-api-major']:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details('API MAJOR VERSION INVALID')
            logging.info("Launch Secret Request Failed: Bad API Major Version")
            return SecretResponse()

        if request.ApiMajor == keyset['min-api-major'] and \
                not request.ApiMinor >= keyset['min-api-minor']:

            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details('API MINOR VERSION INVALID')
            logging.info("Launch Secret Request Failed: Bad API Minor Version")
            return SecretResponse()

        # build
        if not request.BuildId in keyset['allowed-build-ids']:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details('BUILD-ID INVALID')
            logging.info("Launch Secret Request Failed: Bad Build Id")
            return SecretResponse()

        # if the metadata checks out, check the measurement
        connection_certs_path = path.join(certs_path, \
                "connection{}".format(request.ConnectionId))

        # read in the tiktek 
        try:
            with open(path.join(connection_certs_path,"tmp_tk.bin"), 'rb') as f:
                tiktek = f.read()
        except Exception as e:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details('CONNECTION ID INVALID')
            logging.info("Launch Secret Request Failed: Bad Connection Id")
            return SecretResponse()

        TEK=tiktek[0:16]
        TIK=tiktek[16:32]

        launch_measure = request.LaunchMeasurement
        nonce = launch_measure[32:48]
        measure = launch_measure[0:32]

        print("launch_measure: ", launch_measure.hex())
        print("nonce: ", nonce.hex())
        print("measure: ", measure.hex())


        if enable_measurement:
            measurement_valid = False
            # calculate measurement for each digest
            os.chdir(connection_certs_path)

            cmd = "sudo cp {} {}". \
                    format(ovmf_path, connection_certs_path)
            os.system(cmd)

            cmd = "sudo {} --calc_measurement OVMF.fd {} true {} {} {}".format(hag_path, nonce.hex(), request.ApiMajor, request.ApiMinor, request.BuildId)
            print("clac measure cmd: ", cmd)
            os.system(cmd)
            with open("owner_measure.bin", 'rb') as fh:
                calc_measure = fh.read()
                print("calc_measure: ", calc_measure.hex())

            if calc_measure == measure:
                measurement_valid = True

            if not measurement_valid:
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details('MEASUREMENT INVALID')
                logging.info("Launch Secret Request Failed: Bad Measurement")

                print("MEASUREMENT FAILED")
                return SecretResponse()
            else:
                logging.info("Launch Secret Request: Measurement Validated")
                print("MEASUREMENT VALID")

        else:
            logging.warn("Launch Secret Request: Measurement Validation Skipped")

        # confirm the keyset
        keydict = {}
        for keyid in keyset['allowed_keys']:
            if keyid in keys:
                keydict[keyid] = keys[keyid]
                logging.info("Launch Secret Request: Key added ")

        # build the secret blob
        keydict_bytes = json.dumps(keydict).encode()
        with open("test-dict","wb") as f:
            f.write(keydict_bytes)


        guid = "e6f5a162-d67f-4750-a67c-5d065f2a9910"
        secret_entry = construct_secret_entry(guid, keydict_bytes)

        l = 16 + 4 + len(secret_entry)
        # CSV-2 requires rounding to 16
        l = (l + 15) & ~15
        secret = bytearray(l);
        secret[0:16] = UUID('{1e74f542-71dd-4d66-963e-ef4287ff173b}').bytes_le
        secret[16:20] = len(secret).to_bytes(4, byteorder='little')
        secret[20:20+len(secret_entry)] = secret_entry

        # save to file secret.txt as hag expected
        print('Save secret to secret.txt')
        sec_txt = connection_certs_path + "/secret.txt"
        fh=open(sec_txt, 'wb')
        fh.write(secret)
        fh.close()


        # the hag should have a fake owner_measure.bin to do the secret op
        # so we must do below to copy the file into connection_certs_path

        os.chdir(connection_certs_path)

        print('Generate secret data used by qemu')
        cmd = "sudo hag --package_secret {} 0x2".format(build_id)
        os.system(cmd)

        print('Read encrypted secret')
        sbin_path = connection_certs_path + "/secret.bin"
        fh=open(sbin_path, 'rb')
        encrypted_secret=bytearray(fh.read())
        fh.close()


        print('Read secret header')
        sheader_path = connection_certs_path + "/secret_header.bin"
        fh=open(sheader_path, 'rb')
        header=bytearray(fh.read())
        fh.close()


        response = SecretResponse(LaunchSecretHeader = bytes(header), \
                LaunchSecretData = bytes(encrypted_secret))

        logging.debug("Launch Secret Response: {}".format(response))
        return response


if __name__ == "__main__":
    options = cli_parsing()
    main(options)
