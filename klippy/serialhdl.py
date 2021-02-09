# Serial port management for firmware communication
#
# Copyright (C) 2016-2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, threading
import serial, can

import msgproto, chelper, util

class error(Exception):
    pass

class SerialReader:
    BITS_PER_BYTE = 10.
    def __init__(self, reactor):
        self.reactor = reactor
        # Serial port
        self.serial_dev = None
        self.msgparser = msgproto.MessageParser()
        # C interface
        self.ffi_main, self.ffi_lib = chelper.get_ffi()
        self.serialqueue = None
        self.default_cmd_queue = self.alloc_command_queue()
        self.stats_buf = self.ffi_main.new('char[4096]')
        # Threading
        self.lock = threading.Lock()
        self.background_thread = None
        # Message handlers
        self.handlers = {}
        self.register_response(self._handle_unknown_init, '#unknown')
        self.register_response(self.handle_output, '#output')
        # Sent message notification tracking
        self.last_notify_id = 0
        self.pending_notifications = {}
    def _bg_thread(self):
        response = self.ffi_main.new('struct pull_queue_message *')
        while 1:
            self.ffi_lib.serialqueue_pull(self.serialqueue, response)
            count = response.len
            if count < 0:
                break
            if response.notify_id:
                params = {'#sent_time': response.sent_time,
                          '#receive_time': response.receive_time}
                completion = self.pending_notifications.pop(response.notify_id)
                self.reactor.async_complete(completion, params)
                continue
            params = self.msgparser.parse(response.msg[0:count])
            params['#sent_time'] = response.sent_time
            params['#receive_time'] = response.receive_time
            hdl = (params['#name'], params.get('oid'))
            try:
                with self.lock:
                    hdl = self.handlers.get(hdl, self.handle_default)
                    hdl(params)
            except:
                logging.exception("Exception in serial callback")
    def _get_identify_data(self, eventtime):
        # Query the "data dictionary" from the micro-controller
        identify_data = ""
        while 1:
            msg = "identify offset=%d count=%d" % (len(identify_data), 40)
            try:
                params = self.send_with_response(msg, 'identify_response')
            except error as e:
                logging.exception("Wait for identify_response")
                return None
            if params['offset'] == len(identify_data):
                msgdata = params['data']
                if not msgdata:
                    # Done
                    return identify_data
                identify_data += msgdata
    def _start_session(self, serial_dev, serial_fd_type='u', client_id=0):
        self.serial_dev = serial_dev
        self.serialqueue = self.ffi_main.gc(
            self.ffi_lib.serialqueue_alloc(serial_dev.fileno(),
                                           serial_fd_type, client_id),
            self.ffi_lib.serialqueue_free)
        self.background_thread = threading.Thread(target=self._bg_thread)
        self.background_thread.start()
        # Obtain and load the data dictionary from the firmware
        completion = self.reactor.register_callback(self._get_identify_data)
        identify_data = completion.wait(self.reactor.monotonic() + 5.)
        if identify_data is None:
            logging.info("Timeout on connect")
            self.disconnect()
            return False
        msgparser = msgproto.MessageParser()
        msgparser.process_identify(identify_data)
        self.msgparser = msgparser
        self.register_response(self.handle_unknown, '#unknown')
        # Setup baud adjust
        mcu_baud = msgparser.get_constant_float('SERIAL_BAUD', None)
        if mcu_baud is not None:
            baud_adjust = self.BITS_PER_BYTE / mcu_baud
            self.ffi_lib.serialqueue_set_baud_adjust(
                self.serialqueue, baud_adjust)
        receive_window = msgparser.get_constant_int('RECEIVE_WINDOW', None)
        if receive_window is not None:
            self.ffi_lib.serialqueue_set_receive_window(
                self.serialqueue, receive_window)
        return True
    def connect_canbus(self, canbus_uuid, canbus_iface="can0"):
        logging.info("Starting CAN connect")
        start_time = self.reactor.monotonic()
        while 1:
            if self.reactor.monotonic() > start_time + 90.:
                raise error("Unable to connect")
            try:
                canbus_query = CANBusQuery(self.reactor, canbus_uuid,
                                           canbus_iface)
            except (IOError, can.CanError) as e:
                logging.warn("Unable to open CAN port: %s", e)
                self.reactor.pause(self.reactor.monotonic() + 5.)
                continue
            can_fd, can_id = canbus_query.get_can_socket()
            if can_fd is None:
                logging.info("Timeout on connect")
                continue
            ret = self._start_session(can_fd, 'c', can_id)
            if ret:
                break
    def connect_pipe(self, filename):
        logging.info("Starting connect")
        start_time = self.reactor.monotonic()
        while 1:
            if self.reactor.monotonic() > start_time + 90.:
                raise error("Unable to connect")
            try:
                serial_dev = open(filename, 'rb+', buffering=0)
            except IOError as e:
                logging.warn("Unable to open port: %s", e)
                self.reactor.pause(self.reactor.monotonic() + 5.)
                continue
            ret = self._start_session(serial_dev)
            if ret:
                break
    def connect_uart(self, serialport, baud, rts=True):
        # Initial connection
        logging.info("Starting serial connect")
        start_time = self.reactor.monotonic()
        while 1:
            if self.reactor.monotonic() > start_time + 90.:
                raise error("Unable to connect")
            try:
                serial_dev = serial.Serial(baudrate=baud, timeout=0,
                                           exclusive=True)
                serial_dev.port = serialport
                serial_dev.rts = rts
                serial_dev.open()
            except (OSError, IOError, serial.SerialException) as e:
                logging.warn("Unable to open serial port: %s", e)
                self.reactor.pause(self.reactor.monotonic() + 5.)
                continue
            stk500v2_leave(serial_dev, self.reactor)
            ret = self._start_session(serial_dev)
            if ret:
                break
    def connect_file(self, debugoutput, dictionary, pace=False):
        self.serial_dev = debugoutput
        self.msgparser.process_identify(dictionary, decompress=False)
        self.serialqueue = self.ffi_main.gc(
            self.ffi_lib.serialqueue_alloc(self.serial_dev.fileno(), 'f', 0),
            self.ffi_lib.serialqueue_free)
    def set_clock_est(self, freq, last_time, last_clock):
        self.ffi_lib.serialqueue_set_clock_est(
            self.serialqueue, freq, last_time, last_clock)
    def disconnect(self):
        if self.serialqueue is not None:
            self.ffi_lib.serialqueue_exit(self.serialqueue)
            if self.background_thread is not None:
                self.background_thread.join()
            self.background_thread = self.serialqueue = None
        if self.serial_dev is not None:
            self.serial_dev.close()
            self.serial_dev = None
        for pn in self.pending_notifications.values():
            pn.complete(None)
        self.pending_notifications.clear()
    def stats(self, eventtime):
        if self.serialqueue is None:
            return ""
        self.ffi_lib.serialqueue_get_stats(
            self.serialqueue, self.stats_buf, len(self.stats_buf))
        return self.ffi_main.string(self.stats_buf)
    def get_reactor(self):
        return self.reactor
    def get_msgparser(self):
        return self.msgparser
    def get_default_command_queue(self):
        return self.default_cmd_queue
    # Serial response callbacks
    def register_response(self, callback, name, oid=None):
        with self.lock:
            if callback is None:
                del self.handlers[name, oid]
            else:
                self.handlers[name, oid] = callback
    # Command sending
    def raw_send(self, cmd, minclock, reqclock, cmd_queue):
        self.ffi_lib.serialqueue_send(self.serialqueue, cmd_queue,
                                      cmd, len(cmd), minclock, reqclock, 0)
    def raw_send_wait_ack(self, cmd, minclock, reqclock, cmd_queue):
        self.last_notify_id += 1
        nid = self.last_notify_id
        completion = self.reactor.completion()
        self.pending_notifications[nid] = completion
        self.ffi_lib.serialqueue_send(self.serialqueue, cmd_queue,
                                      cmd, len(cmd), minclock, reqclock, nid)
        params = completion.wait()
        if params is None:
            raise error("Serial connection closed")
        return params
    def send(self, msg, minclock=0, reqclock=0):
        cmd = self.msgparser.create_command(msg)
        self.raw_send(cmd, minclock, reqclock, self.default_cmd_queue)
    def send_with_response(self, msg, response):
        cmd = self.msgparser.create_command(msg)
        src = SerialRetryCommand(self, response)
        return src.get_response(cmd, self.default_cmd_queue)
    def alloc_command_queue(self):
        return self.ffi_main.gc(self.ffi_lib.serialqueue_alloc_commandqueue(),
                                self.ffi_lib.serialqueue_free_commandqueue)
    # Dumping debug lists
    def dump_debug(self):
        out = []
        out.append("Dumping serial stats: %s" % (
            self.stats(self.reactor.monotonic()),))
        sdata = self.ffi_main.new('struct pull_queue_message[1024]')
        rdata = self.ffi_main.new('struct pull_queue_message[1024]')
        scount = self.ffi_lib.serialqueue_extract_old(self.serialqueue, 1,
                                                      sdata, len(sdata))
        rcount = self.ffi_lib.serialqueue_extract_old(self.serialqueue, 0,
                                                      rdata, len(rdata))
        out.append("Dumping send queue %d messages" % (scount,))
        for i in range(scount):
            msg = sdata[i]
            cmds = self.msgparser.dump(msg.msg[0:msg.len])
            out.append("Sent %d %f %f %d: %s" % (
                i, msg.receive_time, msg.sent_time, msg.len, ', '.join(cmds)))
        out.append("Dumping receive queue %d messages" % (rcount,))
        for i in range(rcount):
            msg = rdata[i]
            cmds = self.msgparser.dump(msg.msg[0:msg.len])
            out.append("Receive: %d %f %f %d: %s" % (
                i, msg.receive_time, msg.sent_time, msg.len, ', '.join(cmds)))
        return '\n'.join(out)
    # Default message handlers
    def _handle_unknown_init(self, params):
        logging.debug("Unknown message %d (len %d) while identifying",
                      params['#msgid'], len(params['#msg']))
    def handle_unknown(self, params):
        logging.warn("Unknown message type %d: %s",
                     params['#msgid'], repr(params['#msg']))
    def handle_output(self, params):
        logging.info("%s: %s", params['#name'], params['#msg'])
    def handle_default(self, params):
        logging.warn("got %s", params)

# Class to send a query command and return the received response
class SerialRetryCommand:
    def __init__(self, serial, name, oid=None):
        self.serial = serial
        self.name = name
        self.oid = oid
        self.last_params = None
        self.serial.register_response(self.handle_callback, name, oid)
    def handle_callback(self, params):
        self.last_params = params
    def get_response(self, cmd, cmd_queue, minclock=0, reqclock=0):
        retries = 5
        retry_delay = .010
        while 1:
            self.serial.raw_send_wait_ack(cmd, minclock, reqclock, cmd_queue)
            params = self.last_params
            if params is not None:
                self.serial.register_response(None, self.name, self.oid)
                return params
            if retries <= 0:
                self.serial.register_response(None, self.name, self.oid)
                raise error("Unable to obtain '%s' response" % (self.name,))
            reactor = self.serial.reactor
            reactor.pause(reactor.monotonic() + retry_delay)
            retries -= 1
            retry_delay *= 2.

# Class to obtain the CANBus id for a given chip uuid
class CANBusQuery:
    CANBUS_ID_ADMIN = 0x3f0
    CMD_QUERY = 1
    RESP_HAVE_CANID = 33
    def __init__(self, reactor, canbus_uuid, canbus_iface='can0'):
        self.reactor = reactor
        self.canbus_iface = canbus_iface
        try:
            uuid = int(canbus_uuid, 16)
        except ValueError:
            uuid = -1
        if uuid < 0 or uuid > 0xffffffffffff:
            raise error("Invalid CAN uuid")
        self.uuid = [(uuid >> (40 - i*8)) & 0xff for i in range(6)]
        self.bus = self._open_socket(self.CANBUS_ID_ADMIN + 1)
        self.completion = reactor.completion()
        self.fd_hdl = reactor.register_fd(self.bus.fileno(), self._process_data)
        self.timer_hdl = reactor.register_timer(self._send_query, reactor.NOW)
    def _open_socket(self, filter_id):
        filters = [{"can_id": filter_id, "can_mask": 0x7ff, "extended": False}]
        bus = can.interface.Bus(channel=self.canbus_iface, can_filters=filters,
                                bustype='socketcan')
        bus.close = bus.shutdown # XXX
        return bus
    def _disconnect(self):
        if self.fd_hdl is None:
            return
        self.reactor.unregister_fd(self.fd_hdl)
        self.fd_hdl = None
        self.reactor.unregister_timer(self.timer_hdl)
        self.timer_hdl = None
        self.bus.close()
        self.bus = None
    def _process_data(self, eventtime):
        msg = self.bus.recv(0.)
        if (msg is None or msg.arbitration_id != self.CANBUS_ID_ADMIN + 1
            or msg.dlc != 8 or msg.data[0] != self.RESP_HAVE_CANID
            or list(msg.data[1:7]) != self.uuid):
            return
        self._disconnect()
        canbus_id = 0x100 + (msg.data[7] << 1)
        self.completion.complete(canbus_id)
    def _send_query(self, eventtime):
        msg = can.Message(arbitration_id=self.CANBUS_ID_ADMIN,
                          data=[self.CMD_QUERY] + self.uuid,
                          is_extended_id=False)
        self.bus.send(msg)
        return eventtime + 0.100
    def get_can_socket(self):
        canbus_id = self.completion.wait(self.reactor.monotonic() + 5.)
        if canbus_id is None:
            # Timeout
            self._disconnect()
            return None, None
        # Create data socket
        try:
            cb_data = self._open_socket(canbus_id + 1)
        except (IOError, can.CanError) as e:
            logging.warn("Unable to open CAN data port: %s", e)
            return None, None
        return cb_data, canbus_id

# Attempt to place an AVR stk500v2 style programmer into normal mode
def stk500v2_leave(ser, reactor):
    logging.debug("Starting stk500v2 leave programmer sequence")
    util.clear_hupcl(ser.fileno())
    origbaud = ser.baudrate
    # Request a dummy speed first as this seems to help reset the port
    ser.baudrate = 2400
    ser.read(1)
    # Send stk500v2 leave programmer sequence
    ser.baudrate = 115200
    reactor.pause(reactor.monotonic() + 0.100)
    ser.read(4096)
    ser.write('\x1b\x01\x00\x01\x0e\x11\x04')
    reactor.pause(reactor.monotonic() + 0.050)
    res = ser.read(4096)
    logging.debug("Got %s from stk500v2", repr(res))
    ser.baudrate = origbaud

def cheetah_reset(serialport, reactor):
    # Fysetc Cheetah v1.2 boards have a weird stateful circuitry for
    # configuring the bootloader. This sequence takes care of disabling it for
    # sure.
    # Open the serial port with RTS asserted
    ser = serial.Serial(baudrate=2400, timeout=0, exclusive=True)
    ser.port = serialport
    ser.rts = True
    ser.open()
    ser.read(1)
    reactor.pause(reactor.monotonic() + 0.100)
    # Toggle DTR
    ser.dtr = True
    reactor.pause(reactor.monotonic() + 0.100)
    ser.dtr = False
    # Deassert RTS
    reactor.pause(reactor.monotonic() + 0.100)
    ser.rts = False
    reactor.pause(reactor.monotonic() + 0.100)
    # Toggle DTR again
    ser.dtr = True
    reactor.pause(reactor.monotonic() + 0.100)
    ser.dtr = False
    reactor.pause(reactor.monotonic() + 0.100)
    ser.close()

# Attempt an arduino style reset on a serial port
def arduino_reset(serialport, reactor):
    # First try opening the port at a different baud
    ser = serial.Serial(serialport, 2400, timeout=0, exclusive=True)
    ser.read(1)
    reactor.pause(reactor.monotonic() + 0.100)
    # Then toggle DTR
    ser.dtr = True
    reactor.pause(reactor.monotonic() + 0.100)
    ser.dtr = False
    reactor.pause(reactor.monotonic() + 0.100)
    ser.close()
