#!/usr/bin/env python2
# Tool to assign CAN ids to nodes
#
# Copyright (C) 2021 Pontus Borg <glpontus@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import time
import can

#################################################################
#
# Debug code to decode a CAN error frame

def decode_crtl(d1):
    msg = ""
    if(d1 & 0x01): msg += " RX_OVERFLOW" # RX buffer overflow
    if(d1 & 0x02): msg += " TX_OVERFLOW" # TX buffer overflow
    if(d1 & 0x04): msg += " RX_WARNING"  # reached warning level for RX errors
    if(d1 & 0x08): msg += " TX_WARNING"  # reached warning level for TX errors
    if(d1 & 0x10): msg += " RX_PASSIVE"  # reached error passive status RX
    if(d1 & 0x20): msg += " TX_PASSIVE"  # reached error passive status TX
    return ""

def decode_prot(d2, d3):
    msg = ""
#d2
    if (d2 & 0x01): msg += " BIT "        # single bit error */
    if (d2 & 0x02): msg += " FORM"        # frame format error */
    if (d2 & 0x04): msg += " STUFF"       # bit stuffing error */
    if (d2 & 0x08): msg += " BIT0"        # unable to send dominant bit */
    if (d2 & 0x10): msg += " BIT1"        # unable to send recessive bit */
    if (d2 & 0x20): msg += " OVERLOAD"    # bus overload */
    if (d2 & 0x40): msg += " ACTIVE"      # active error announcement */
    if (d2 & 0x80): msg += " TX"          # error occurred on transmission */

    if (d3 == 0x03): msg += " LOC_SOF"      # start of frame */
    if (d3 == 0x02): msg += " LOC_ID28_21"  # ID bits 28 - 21 (SFF: 10 - 3) */
    if (d3 == 0x06): msg += " LOC_ID20_18"  # ID bits 20 - 18 (SFF: 2 - 0 )*/
    if (d3 == 0x04): msg += " LOC_SRTR"     # substitute RTR (SFF: RTR) */
    if (d3 == 0x05): msg += " LOC_IDE"      # identifier extension */
    if (d3 == 0x07): msg += " LOC_ID17_13"  # ID bits 17-13 */
    if (d3 == 0x0F): msg += " LOC_ID12_05"  # ID bits 12-5 */
    if (d3 == 0x0E): msg += " LOC_ID04_00"  # ID bits 4-0 */
    if (d3 == 0x0C): msg += " LOC_RTR"      # RTR */
    if (d3 == 0x0D): msg += " LOC_RES1"     # reserved bit 1 */
    if (d3 == 0x09): msg += " LOC_RES0"     # reserved bit 0 */
    if (d3 == 0x0B): msg += " LOC_DLC"      # data length code */
    if (d3 == 0x0A): msg += " LOC_DATA"     # data section */
    if (d3 == 0x08): msg += " LOC_CRC_SEQ"  # CRC sequence */
    if (d3 == 0x18): msg += " LOC_CRC_DEL"  # CRC delimiter */
    if (d3 == 0x19): msg += " LOC_ACK"      # ACK slot */
    if (d3 == 0x1B): msg += " LOC_ACK_DEL"  # ACK delimiter */
    if (d3 == 0x1A): msg += " LOC_EOF"      # end of frame */
    if (d3 == 0x12): msg += " LOC_INTERM"   # intermission */
    return msg

def decode_trx(d4):
    return ""

def dump_error_frame(message):
    canid = message.arbitration_id
    msg = ""
    if(canid & 0x1): msg += "TX_TIMEOUT " # TX timeout (by netdevice driver)
    if(canid & 0x2): msg += "LOSTARB "        # lost arbitration    / data[0]
    if(canid & 0x4) : msg += "CRTL("  + decode_crtl(message.data[1]) + ") "
    if(canid & 0x8) : msg += "PROT(" + decode_prot(message.data[2],
                                                   message.data[3]) + ") "
    if(canid & 0x10) : msg += "TRX("  + decode_trx(message.data[4]) + ") "
    if(canid & 0x20) : msg += "ACK "    # received no ACK on transmission */
    if(canid & 0x40): msg += " BUSOFF " # bus off */
    if(canid & 0x80) : msg += "BUSERROR "  # bus error (may flood!) */
    if(canid & 0x100) : msg += "RESTARTED " # controller restarted */
    print(message)
    print(msg)


#################################################################
#
# A very simple CANID assigning program
#
# Frame format
#  <1 byte cmd><1 byte encoded_can_id><6 byte uuid>
#
# Host to node commands:
#   0  QUERY_ALL    (just 1 byte data)
#   1  NODEID_QUERY
#   2  NODEID_SET
#   3  RESET
#   4  ENTER_BOOTLAODER
#
# Node to host commands:
#   0  NONODEID
#   1  NODEID


HOST_TO_NODE_ID = 0x3f0
NODE_TO_HOST_ID = 0x3f1

CMD_QUERY_ALL    = 0
CMD_NODEID_QUERY = 1
CMD_NODEID_SET   = 2
CMD_RESET        = 3
CMD_BOOTLOADER   = 4

RESP_NEED_CANID = 32
RESP_HAVE_CANID = 33

class CanNode:
    def __init__(self, uuid, nodeid, timestamp):
        self.uuid = uuid
        self.nodeid = nodeid
        self.timestamp = timestamp

class CanNodes:
    def __init__(self):
        self.nodes = dict() # main dict with UUID=>CanNode
        self.next_unused_nodeid = 0x80

    def get_node(self, uuid):
        if not uuid in self.nodes:
            return None
        return self.nodes[uuid]

    def add_node(self, uuid, nodeid, timestamp):
        node = CanNode(uuid, nodeid, timestamp)
        self.nodes[uuid] = node
        return node

    def find_unused_nodeid(self):
        id = self.next_unused_nodeid
        self.next_unused_nodeid+=2 # TODO, better allocation
        return id

def send_query_all(bus):
    msg = can.Message(arbitration_id=HOST_TO_NODE_ID,
                      data=[CMD_QUERY_ALL],
                      is_extended_id=False)
    bus.send(msg)

def send_query(bus, uuid):
    msg = can.Message(arbitration_id=HOST_TO_NODE_ID,
                      data=[CMD_NODEID_QUERY,
                            uuid[0], uuid[1], uuid[2],
                            uuid[3], uuid[4], uuid[5]],
                      is_extended_id=False)
    bus.send(msg)

def send_set_nodeid(bus, nodeid, uuid):

    msg = can.Message(arbitration_id=HOST_TO_NODE_ID,
                      data=[CMD_NODEID_SET,
                            uuid[0], uuid[1], uuid[2],
                            uuid[3], uuid[4], uuid[5], nodeid],
                      is_extended_id=False)
    bus.send(msg)

def send_reset(bus, uuid):
    msg = can.Message(arbitration_id=HOST_TO_NODE_ID,
                      data=[CMD_RESET,
                            uuid[0], uuid[1], uuid[2],
                            uuid[3], uuid[4], uuid[5]],
                      is_extended_id=False)
    bus.send(msg)

def send_bootloader(bus, uuid):
    msg = can.Message(arbitration_id=HOST_TO_NODE_ID,
                      data=[CMD_BOOTLOADER,
                            uuid[0], uuid[1], uuid[2],
                            uuid[3], uuid[4], uuid[5]],
                      is_extended_id=False)
    bus.send(msg)


def uuidstr(uuid):
    return "%02x%02x%02x%02x%02x%02x" % (uuid[0], uuid[1], uuid[2],
                                         uuid[3], uuid[4], uuid[5])


def nodeidadmin(iface):
    """."""
    filters = [{"can_id": NODE_TO_HOST_ID, "can_mask": 0xffc,
                "extended": False}]
    bustype = 'socketcan'

    nodes = CanNodes()  # keyed by byte array[6] UUID

    bus = can.interface.Bus(channel=iface,can_filters=filters, bustype=bustype)

    send_query_all(bus)

    while True:
        message = bus.recv(5.0)  # Timeout in seconds.
        if( message is None):
            t = time.time()
            # TODO: Use a better method for timekeeping and timeouts
            # TODO: Check for nodes that have not responded in a long time,
            # and free their ID

            # This is strictly not necessary as nodes should make
            # themselves heard when missing CANID
            send_query_all(bus)
        else:
            if(message.is_error_frame):
                #dump_error_frame(message)
                pass
            elif message.arbitration_id == NODE_TO_HOST_ID and message.dlc == 8:
                # print(message)
                cmd   = message.data[0]
                nodeid = message.data[7]
                uuid  = (message.data[1], message.data[2], message.data[3],
                         message.data[4], message.data[5], message.data[6])

                if cmd == RESP_NEED_CANID: # An unassigned node
                    # Need to assign a nodeid to the node
                    node = nodes.get_node(uuid)
                    if node is None:
                        nodeid = nodes.find_unused_nodeid()
                        node = nodes.add_node(uuid, nodeid, message.timestamp)
                    print("Node " + uuidstr(node.uuid)
                          + " nodeid %x" % node.nodeid)
                    send_set_nodeid(bus, node.nodeid, node.uuid)

                if cmd == RESP_HAVE_CANID: # An assigned node
                    node = nodes.get_node(uuid)
                    if node is None:
                        # An old already assigned node that we have no record of

                        # TODO: We could accept this, but it's easier to just
                        # reset the node
                        print("Old  " + uuidstr(uuid) + ". Reset it")
                        send_reset(bus, uuid)
                    else:
                        if node.nodeid != nodeid:
                            print(" node has wrong nodeid, reset")
                            send_reset(bus, uuid)
                        else:
                            node.timestamp = message.timestamp

nodeidadmin('can0')
