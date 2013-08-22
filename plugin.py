###
# This file is part of Soap.
#
# Soap is free software; you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, version 2.
#
# Soap is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.
#
# See the GNU General Public License for more details. You should have received
# a copy of the GNU General Public License along with Soap. If not, see
# <http://www.gnu.org/licenses/>.
###

import supybot.conf as conf
import supybot.utils as utils
import supybot.ircdb as ircdb
import supybot.ircmsgs as ircmsgs
from supybot.commands import *
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
import supybot.plugins as plugins

import threading
import time
import socket

from soapclient import SoapClient
from libottdadmin2.trackingclient import poll, POLLIN, POLLOUT, POLLERR, POLLHUP, POLLPRI, POLL_MOD
from libottdadmin2.constants import *
from libottdadmin2.enums import *
from libottdadmin2.packets import *


class Soap(callbacks.Plugin):
    """
    This plug-in allows supybot to interface to OpenTTD via its built-in
    adminport protocol
    """

    def __init__(self, irc):
        self.__parent = super(Soap, self)
        self.__parent.__init__(irc)
        self.irc = irc
        self._pollObj = poll()
        self.channels = self.registryValue('channels')
        self.connections = []
        for channel in self.channels:
            self.log.info ('looping %s' % channel)
            conn = SoapClient()
            self._attachEvents(conn)
            self._initSoapClient(conn, irc, channel)
            self._pollObj.register(conn.fileno(), POLLIN | POLLERR | POLLHUP | POLLPRI)
            self.log.info('registering %s with fileno: %s' % (conn.ID, conn.fileno()))
            self.connections.append(conn)
        self.stopPoll = threading.Event()
        self.pollingThread = threading.Thread(target = self._pollThread)
        self.pollingThread.daemon = True
        self.pollingThread.start()

    def _pollThread(self):
        timeout = 1.0

        while True:
            polList = []
            for conn in self.connections:
                if conn.is_connected and conn._poll_registered:
                    polList.append(conn.fileno())
            # lets not use up 100% cpu if there are no active connections
            if len(polList) == 0:
                time.sleep(1)
            else:
                self.log.info('%s active connections, list: %s' % (len(polList), polList))
                events = self._pollObj.poll(timeout * POLL_MOD)
                for fileno, event in events:
                    self.log.info('fileno: %s - event: %s' % (fileno, event))
                    if polList.count(fileno) == 0:
                        continue
                    conn = self._getConnection('fileno', fileno)
                    if (event & POLLIN) or (event & POLLPRI):
                        packet = conn.recv_packet()
                        if packet == None:
                            polList.remove(fileno)
                            self._disconnect(conn, True)
                            continue
                    elif (event & POLLERR) or (event & POLLHUP):
                        polList.remove(fileno)
                        self._disconnect(conn, True)
            if self.stopPoll.isSet():
                break
            self.log.info('reached end of loop')
        self.log.info('Terminating loop. I\'ll be back')

    def die(self):
        try:
            for conn in self.connections:
                self.log.info('Disconnecting %s' % conn.ID)
                conn.disconnect()
            self.stopPoll.set()
            self.log.info('Thread event set, waiting for thread to join')
            self.pollingThread.join()
            self.log.info('Thread joined')
        except NameError:
            pass



    # Connection management

    def _attachEvents(self, conn):
        conn.soapEvents.chat            += self._rcvChat
        conn.soapEvents.clientjoin      += self._rcvClientJoin
        conn.soapEvents.clientquit      += self._rcvClientQuit
        conn.soapEvents.clientupdate    += self._rcvClientUpdate

    def _connectOTTD(self, irc, conn, source = None):
        self._initSoapClient(conn, irc, conn.channel)
        conn.connect()
        text = 'Connecting...'
        self._msgChannel(irc, conn.channel, text)
        if not source == None and not source == conn.channel.lower():
            self._msgChannel(irc, source, text)


    def _initSoapClient(self, conn, irc, channel):
        conn.configure(
            irc         = irc,
            ID          = self.registryValue('serverID', channel),
            password    = self.registryValue('password', channel),
            host        = self.registryValue('host', channel),
            port        = self.registryValue('port', channel),
            channel     = channel,
            autoConnect = self.registryValue('autoConnect', channel),
            allowOps    = self.registryValue('allowOps', channel),
            playAsPlayer = self.registryValue('playAsPlayer', channel),
            name        = '%s-Soap' % irc.nick)

    def _disconnect(self, conn, forced):
        try:
            self._pollObj.unregister(conn.fileno())
        except KeyError:
            pass
        if forced:
            conn.force_disconnect()
        else
            conne.disconnect()



    # Miscelanious functions

    def _checkPermission(self, irc, msg, channel, allowOps):
        capable = ircdb.checkCapability(msg.prefix, 'trusted')
        if capable:
            return True
        else:
            opped = msg.nick in irc.state.channels[channel].ops
            if opped and allowOps:
                return True
            else:
                return False

    def _getConnection(self, which, connID):
        if which == 'channel':
            for conn in self.connections:
                if conn.channel == connID:
                    return conn
        elif which == 'ID':
            for conn in self.connections:
                if conn.ID == connID:
                    return conn
        elif which == 'fileno':
            for conn in self.connections:
                if conn.fileno() == connID:
                    return conn
        return None

    def _msgChannel(self, irc, channel, msg):
        if channel in irc.state.channels or irc.isNick(channel):
            irc.queueMsg(ircmsgs.privmsg(channel, msg))

    def _movePlayer(self, irc, conn, client):
        command = 'move %s 255' % client.id

        self.send_packet(AdminRcon, command = command)
        text = 'Please change your name before joining/starting a company'
        conn.send_packet(AdminChat,
            action = Action.CHAT_CLIENT,
            destType = DestType.CLIENT,
            clientID = client.id,
            message = text)
        text = '[private] -> %s: %s' % (client.name, text)
        self._msgChannel(irc, conn._channel, text)



    # Packet Handlers

    def _rcvChat(self, irc, conn, client, action, destType, clientID, message, data):
        clientName = str(clientID)
        clientCompany = None
        if client != clientID:
            clientName = client.name
            clientCompany = conn.companies.get(client.play_as, None)
        if clientCompany:
            companyName = clientCompany.name
            companyID = clientCompany.id + 1
        else:
            companyName = 'Unknown'
            companyID = '?'

        if action == Action.CHAT:
            text = '<%s> %s' % (clientName, message)
            self._msgChannel(irc, conn._channel, text)
        elif action == Action.CHAT_COMPANY or action == Action.CHAT_CLIENT:
            pass
        elif action == Action.COMPANY_SPECTATOR:
            text = '*** %s has joined spectators' % clientName
            self._msgChannel(irc, conn._channel, text)
        elif action == Action.COMPANY_JOIN:
            text = '*** %s has joined %s (Company #%s)' % (clientName, companyName, companyID)
            self._msgChannel(irc, conn._channel, text)
            if not conn.playAsPlayer and 'player' in clientName.lower():
                self._movePlayer(irc, conn, client)
        elif action == Action.COMPANY_NEW:
            text = '*** %s had created a new company: %s(Company #%s)' % (clientName, companyName, companyID)
            self._msgChannel(irc, conn._channel, text)
            if not conn.playAsPlayer and 'player' in clientName.lower():
                self._movePlayer(irc, conn, client)
        else:
            text = 'AdminChat: Action %r, DestType %r, name %s, companyname %s, message %r, data %r' % (
                action, destType, clientName, companyName, message, data)
            self._msgChannel(irc, conn._channel, text)

    def _rcvClientJoin(self, irc, conn, client):
        if isinstance(client, (long, int)):
            return
        text = '*** %s (Client #%s) has joined the game' % (client.name, client.id)
        self._msgChannel(conn._irc, conn._channel, text)

    def _rcvClientQuit(self, irc, conn, client, errorcode):
        if isinstance(client, (long, int)):
            return
        text = '*** %s (Client #%s) has left the game (leaving)' % (client.name, client.id)
        self._msgChannel(conn._irc, conn._channel, text)

    def _rcvClientUpdate(self, irc, conn, old, client, changed):
        if 'name' in changed:
            text = "*** %s is now known as %s" % (old.name, client.name)
            self._msgChannel(conn._irc, conn._channel, text)



    # IRC commands

    def apconnect(self, irc, msg, args, serverID = None):
        """ no arguments

        connect to AdminPort of OpenTTD server
        """

        source = msg.args[0].lower()
        conn = None
        if not serverID is None:
            conn = self._getConnection('ID', serverID)
        else:
            if irc.isChannel(source) and source in self.channels:
                conn = self._getConnection('channel', source)
        if conn == None:
            return

        if self._checkPermission(irc, msg, conn.channel, conn.allowOps):
            if conn.is_connected:
                irc.reply('Already connected!!', prefixNick = False)
            else:
                try:
                    self._pollObj.unregister(conn.fileno())
                except KeyError:
                    pass
                except IOError:
                    pass
                conn = conn.copy()
                for i, c in enumerate(self.connections):
                    if c.ID == conn.ID:
                        self.connections[i] = conn
                        break
                self._initSoapClient(conn, irc, conn.channel)
                self.log.info('registering %s with fileno: %s' % (conn.ID, conn.fileno()))
                self._pollObj.register(conn.fileno(), POLLIN | POLLERR | POLLHUP | POLLPRI)
                self._connectOTTD(irc, conn, source)
    apconnect = wrap(apconnect, [optional('text')])

    def apdisconnect(self, irc, msg, args, serverID = None):
        """ no arguments

        disconnect from server
        """

        source = msg.args[0].lower()
        conn = None
        if not serverID is None:
            conn = self._getConnection('ID', serverID)
        else:
            if irc.isChannel(source) and source in self.channels:
                conn = self._getConnection('channel', source)
        if conn == None:
            return

        if self._checkPermission(irc, msg, conn.channel, conn.allowOps):
            if conn.is_connected:
                irc.reply('Disconnecting')
                self._disconnect(conn, False)
                if not conn.is_connected:
                    irc.reply('Disconnected', prefixNick = False)
            else:
                irc.reply('Not connected!!', prefixNick = False)
    apdisconnect = wrap(apdisconnect, [optional('text')])

    def rcon(self, irc, msg, args, command):
        """ <rcon command>

        sends a rcon command to openttd
        """

        source = msg.args[0].lower()
        conn = None
        serverID = None
        if not serverID is None:
            conn = self._getConnection('ID', serverID)
        else:
            if irc.isChannel(source) and source in self.channels:
                conn = self._getConnection('channel', source)
        if conn == None:
            return

        if self._checkPermission(irc, msg, conn.channel, conn.allowOps):
            if not conn.is_connected:
                irc.reply('Not connected!!', prefixNick = False)
                return
            if len(command) >= NETWORK_RCONCOMMAND_LENGTH:
                message = "RCON Command too long (%d/%d)" % (len(command), NETWORK_RCONCOMMAND_LENGTH)
                irc.reply(message, prefixNick = False)
                return
            else:
                conn.send_packet(AdminRcon, command = command)
    rcon = wrap(rcon, ['text'])

    def pause(self, irc, msg, args, serverID = None):
        """ takes no arguments

        pauses the game server
        """

        source = msg.args[0].lower()
        conn = None
        if not serverID is None:
            conn = self._getConnection('ID', serverID)
        else:
            if irc.isChannel(source) and source in self.channels:
                conn = self._getConnection('channel', source)
        if conn == None:
            return

        if self._checkPermission(irc, msg, conn.channel, conn.allowOps):
            if not conn.is_connected:
                irc.reply('Not connected!!', prefixNick = False)
                return
            command = 'pause'
            conn.send_packet(AdminRcon, command = command)
    pause = wrap(pause, [optional('text')])

    def unpause(self, irc, msg, args, serverID = None):
        """ takes no arguments

        unpauses the game server, or if min_active_clients > 1, changes the server to autopause mode
        """

        source = msg.args[0].lower()
        conn = None
        if not serverID is None:
            conn = self._getConnection('ID', serverID)
        else:
            if irc.isChannel(source) and source in self.channels:
                conn = self._getConnection('channel', source)
        if conn == None:
            return

        if self._checkPermission(irc, msg, conn.channel, conn.allowOps):
            if not conn.is_connected:
                irc.reply('Not connected!!', prefixNick = False)
                return
            command = 'unpause'
            conn.send_packet(AdminRcon, command = command)
    unpause = wrap(unpause, [optional('text')])



    # Relay IRC back ingame

    def doPrivmsg(self, irc, msg):

        (source, text) = msg.args
        conn = None
        source = source.lower()
        if irc.isChannel(source) and source in self.channels:
            conn = self._getConnection('channel', source)
        if conn == None:
            return

        actionChar = conf.get(conf.supybot.reply.whenAddressedBy.chars, source)
        if actionChar in text[:1]:
            return
        if not 'ACTION' in text:
            message = 'IRC <%s> %s' % (msg.nick, text)
        else:
            text = text.split(' ',1)[1]
            text = text[:-1]
            message = 'IRC ** %s %s' % (msg.nick, text)
        conn.send_packet(AdminChat,
            action = Action.CHAT,
            destType = DestType.BROADCAST,
            clientID = ClientID.SERVER,
            message = message)

Class = Soap

# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
