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

from libottdadmin2.trackingclient import *
from libottdadmin2.constants import *
from libottdadmin2.enums import *
from libottdadmin2.packets import *


class SoapClient(TrackingAdminClient):
    _settable_args = TrackingAdminClient._settable_args + ['conName', 'channel', 'autoConnect', 'allowOps', 'playAsPlayer']
    _conName = 'Default'
    _channel = None
    _autoConnect = False
    _allowOps = False
    _playAsPlayer = True

    @property
    def conName(self):
        return self._conName

    @conName.setter
    def conName(self, value):
        self._conName = value

    @property
    def channel(self):
        return self._channel

    @channel.setter
    def channel(self, value):
        self._channel = value.lower()

    @property
    def autoConnect(self):
        return self._autoConnect

    @autoConnect.setter
    def autoConnect(self, value):
        self._autoConnect = value

    @property
    def allowOps(self):
        return self._allowOps

    @allowOps.setter
    def allowOps(self, value):
        self._allowOps = value

    @property
    def playAsPlayer(self):
        return self._playAsPlayer

    @playAsPlayer.setter
    def playAsPlayer(self, value):
        self._playAsPlayer = value

class Soap(callbacks.Plugin):
    """
    This plug-in allows supybot to interface to OpenTTD via its built-in
    adminport protocol
    """

    def __init__(self, irc):
        self.__parent = super(Soap, self)
        self.__parent.__init__(irc)
        self.polling = False
        self.irc = irc
        self._createSoapClient(irc)
        if self.connection.channel in irc.state.channels and self.connection.autoConnect:
            self._connectOTTD(irc)

    def die(self):
        self.connection.disconnect()

    def doJoin(self, irc, msg):
        if (msg.nick == irc.nick
            and msg.args[0].lower() == self.connection.channel
            and self.connection.autoConnect
            and not self.connection.is_connected):

            self._connectOTTD(irc)



    # Connection management

    def _connectOTTD(self, irc, source = None):
        source = source.lower()
        success = self._initializeConnection(irc)
        c = self.connection
        if success:
            self.log.info('Starting to listen')
            t = threading.Timer(0.1, self._pollForData, args=[irc, c])
            t.daemon = True
            t.start()
            while c.serverinfo.name == None:
                pass
            text = 'Connected to %s(%s)' % (c.serverinfo.name,
                c.serverinfo.version)
        else:
            text = 'Connection failed.'
        self._msgChannel(irc, c.channel, text)
        if not source == None and not source == c.channel:
            self._msgChannel(irc, source, text)

    def _initializeConnection(self, irc):
        self._createSoapClient(irc)
        self.connection.connect()
        protocol_response = None
        try:
            protocol_response = self.connection.recv_packet()
            if protocol_response is None:
                self.log.info('no response from server')
                return False
        except socket.error, v:
            self.log.info('connection error: %s' % v)
            return False
        except NameError, v:
            self.log.info('Name error %s' %v)
            return False
        else:
            return True

    def _createSoapClient(self, irc):
        self.connection = SoapClient()
        self._attachEvents(self.connection)
        self.connection.configure(
            password    = self.registryValue('password'),
            host        = self.registryValue('host'),
            port        = self.registryValue('port'),
            timeout     = float(0.5),
            channel     = self.registryValue('channel'),
            autoConnect = self.registryValue('autoConnect'),
            allowOps    = self.registryValue('allowOps'),
            playAsPlayer = self.registryValue('playAsPlayer'),
            name        = '%s-Soap' % irc.nick)

    def _attachEvents(self, c):
        c.events.chat           += self._rcvChat

        c.events.clientjoin     += self._rcvClientJoin
        c.events.clientquit     += self._rcvClientQuit
        c.events.clientupdate   += self._rcvClientUpdate

    def _pollForData(self, irc, c):
        c = self.connection

        if not c.is_connected:
            self.log.info ('Stopped listening')
            return
        try:
            packets = c.poll(0.5)
        except Exception, e:
            self.log.info('exception caught: %s' % str(e))
            self.log.info ('Stopped listening')
            if c.is_connected:
                c.disconnect()
                text = 'Error encountered, disconnecting. Contact administrator if this keeps happening'
                self._msgChannel(irc, self.connection.channel, text)
            return
        t = threading.Timer(0.1, self._pollForData, args=[irc, c])
        t.daemon = True
        t.start()



    # Packet Handlers

    def _rcvChat(self, client, action, destType, clientID, message, data):
        irc = self.irc
        c = self.connection
        clientName = str(clientID)
        clientCompany = None
        if client != clientID:
            clientName = client.name
            clientCompany = c.companies.get(client.play_as, None)
        if clientCompany:
            companyName = clientCompany.name
            companyID = clientCompany.id + 1
        else:
            companyName = 'Unknown'
            companyID = '?'

        if action == Action.CHAT:
            text = '<%s> %s' % (clientName, message)
            self._msgChannel(irc, c.channel, text)
        elif action == Action.CHAT_COMPANY or action == Action.CHAT_CLIENT:
            pass
        elif action == Action.COMPANY_SPECTATOR:
            text = '*** %s has joined spectators' % clientName
            self._msgChannel(irc, c.channel, text)
        elif action == Action.COMPANY_JOIN:
            text = '*** %s has joined %s (Company #%s)' % (clientName, companyName, companyID)
            self._msgChannel(irc, c.channel, text)
            if not self.connection.playAsPlayer and 'player' in clientName.lower():
                self._movePlayer(irc, c, client)
        elif action == Action.COMPANY_NEW:
            text = '*** %s had created a new company: %s(Company #%s)' % (clientName, companyName, companyID)
            self._msgChannel(irc, c.channel, text)
            if not self.connection.playAsPlayer and 'player' in clientName.lower():
                self._movePlayer(irc, c, client)
        else:
            text = 'AdminChat: Action %r, DestType %r, name %s, companyname %s, message %r, data %s' % (
                action, destType, clientName, companyName, message, data)
            self._msgChannel(irc, c.channel, text)
            
    def _rcvClientJoin(self, client):
        irc = self.irc
        if isinstance(client, (long, int)):
            return
        text = '*** %s (Client #%s) has joined the game' % (client.name, client.id)

        self._msgChannel(irc, self.connection.channel, text)

    def _rcvClientQuit(self, client, errorcode):
        irc = self.irc
        if isinstance(client, (long, int)):
            return
        text = '*** %s (Client #%s) has Quit the game' % (client.name, client.id)

        self._msgChannel(irc, self.connection.channel, text)

    def _rcvClientUpdate(self, old, client, changed):
        irc = self.irc
        c = self.connection

        if 'name' in changed:
            text = "*** %s is now known as %s" % (old.name, client.name)
            self._msgChannel(irc, c.channel, text)



    # Miscelanious functions

    def _movePlayer(self, irc, c, client):
        command = 'move %s 255' % client.id
        c.send_packet(AdminRcon, command = command)
        text = 'Please change your name before joining/starting a company'
        c.send_packet(AdminChat,
            action = Action.CHAT_CLIENT,
            destType = DestType.CLIENT,
            clientID = client.id,
            message = text)
        text = '[private] -> %s: %s' % (client.name, text)
        self._msgChannel(irc, c.channel, text)

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

    def _msgChannel(self, irc, channel, msg):
        if channel in irc.state.channels or irc.isNick(channel):
            irc.queueMsg(ircmsgs.privmsg(channel, msg))



    # IRC commands

    def apconnect(self, irc, msg, args):
        """ no arguments

        connect to AdminPort of OpenTTD server
        """

        source = msg.args[0].lower()
        if irc.isChannel(source) and not source == self.connection.channel:
            return
        if self._checkPermission(irc, msg, self.connection.channel, self.connection.allowOps):
            if self.connection.is_connected:
                irc.reply('Already connected!!', prefixNick = False)
            else:
                if irc.isChannel(msg.args[0]):
                    source = msg.args[0]
                else:
                    source = msg.nick
                irc.reply('Connecting', prefixNick = False)
                self._connectOTTD(irc, source = source)
    apconnect = wrap(apconnect)

    def apdisconnect(self, irc, msg, args):
        """ no arguments

        disconnect from server
        """

        source = msg.args[0].lower()
        if irc.isChannel(source) and not source == self.connection.channel:
            return
        if self._checkPermission(irc, msg, self.connection.channel, self.connection.allowOps):
            if self.connection.is_connected:
                irc.reply('Disconnecting')
                self.connection.disconnect()
                if not self.connection.is_connected:
                    irc.reply('Disconnected', prefixNick = False)
            else:
                irc.reply('Not connected!!', prefixNick = False)
    apdisconnect = wrap(apdisconnect)

    def rcon(self, irc, msg, args, command):
        """ <rcon command>

        sends a rcon command to openttd
        """

        source = msg.args[0].lower()
        if irc.isChannel(source) and not source == self.connection.channel:
            return
        if self._checkPermission(irc, msg, self.connection.channel, self.connection.allowOps):
            if not self.connection.is_connected:
                irc.reply('Not connected!!', prefixNick = False)
                return
            if len(command) >= NETWORK_RCONCOMMAND_LENGTH:
                message = "RCON Command too long (%d/%d)" % (len(command), NETWORK_RCONCOMMAND_LENGTH)
                irc.reply(message, prefixNick = False)
                return
            self.connection.send_packet(AdminRcon, command = command)
        else:
            irc.reply('Not connected!!', prefixNick = False)
    rcon = wrap(rcon, ['text'])

    def pause(self, irc, msg, args):
        """ takes no arguments

        pauses the game server
        """

        source = msg.args[0].lower()
        if irc.isChannel(source) and not source == self.connection.channel:
            return
        if self._checkPermission(irc, msg, self.connection.channel, self.connection.allowOps):
            if not self.connection.is_connected:
                irc.reply('Not connected!!', prefixNick = False)
                return
            command = 'pause'
            self.connection.send_packet(AdminRcon, command = command)
    pause = wrap(pause)

    def unpause(self, irc, msg, args):
        """ takes no arguments

        unpauses the game server, or if min_active_clients > 1, changes the server to autopause mode
        """

        source = msg.args[0].lower()
        if irc.isChannel(source) and not source == self.connection.channel:
            return
        if self._checkPermission(irc, msg, self.connection.channel, self.connection.allowOps):
            if not self.connection.is_connected:
                irc.reply('Not connected!!', prefixNick = False)
                return
            command = 'unpause'
            self.connection.send_packet(AdminRcon, command = command)
    unpause = wrap(unpause)



    # Relay IRC back ingame

    def doPrivmsg(self, irc, msg):
        (channel, text) = msg.args
        if not channel.lower() == self.connection.channel:
            return
        actionChar = conf.get(conf.supybot.reply.whenAddressedBy.chars, channel)
        if actionChar in text[:1]:
            return
        if channel == self.connection.channel:
            if not 'ACTION' in text:
                message = 'IRC <%s> %s' % (msg.nick, text)
            else:
                text = text.split(' ',1)[1]
                text = text[:-1]
                message = 'IRC * %s %s' % (msg.nick, text)
            self.connection.send_packet(AdminChat,
                action = Action.CHAT,
                destType = DestType.BROADCAST,
                clientID = ClientID.SERVER,
                message = message)
        
Class = Soap

# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
