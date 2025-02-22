import os
import os.path
import imp
import subprocess
import sys
from functools import wraps
from threading import Thread
from time import sleep
import neovim
import urllib.request, urllib.parse, urllib.error
urllib = imp.reload(urllib)
from urllib import request
request = imp.reload(request)
Request = request.Request
urlopen = request.urlopen
HTTPError = urllib.error.HTTPError
URLError = urllib.error.URLError


from .common import shared as S
from .common import reactor, msg, utils
from . import editor
from . import vui
from . import view
from . import vim_handler


VUI = vui.VUI()

reactor = reactor.reactor

# Protocol version
S.__VERSION__ = '0.11'
S.__PLUGIN_VERSION__ = '3.4.4'

S.LOG_TO_CONSOLE = False
S.CHAT_VIEW = True

msg.editor_log = msg.floobits_log

utils.reload_settings()


class EventLoop(Thread):
    def __init__(self, vim, ticker):
        super(EventLoop, self).__init__()
        self.vim = vim
        self.ticker = ticker
        self.intervals = []

    def run(self):
        msg.log("Starting event loop.")
        while True:
            sleep(0.1)
            self.vim.session.threadsafe_call(self.tick)

    def tick(self):
        try:
            self.ticker()
        except Exception as e:
            msg.log("Event loop tick error: %s" % e)


def leave_follow_mode():
    S.FOLLOW_USERS.clear()
    S.FOLLOW_MODE = None


def is_connected(warn=False):
    def outer(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            if not reactor.is_ready():
                return
            return func(*args, **kwargs)
        return wrapped
    return outer


@utils.inlined_callbacks
def check_credentials():
    msg.debug('Print checking credentials.')
    if utils.can_auth():
        return
    if not utils.has_browser():
        msg.log('You need a Floobits account to use the Floobits plugin. Go to https://floobits.com to sign up.')
        return
    yield VUI.create_or_link_account, None, S.DEFAULT_HOST, False


@neovim.plugin
class Floobits(object):
    def __init__(self, vim):
        self.vim = vim
        vui.vim = vim
        editor.vim = vim
        view.vim = vim
        vim_handler.vim = vim
        self.eventLoop = EventLoop(vim, self.tick)
        reactor.on_stop = self.on_stop

    def on_stop(self):
        self.vim.command('let g:floo_connected = 0')

    def tick(self):
        reactor.tick()

    def start_ticker(self):
        if not self.eventLoop.is_alive():
            self.eventLoop.start()
        if not utils.can_auth():
            check_credentials()
            return False
        return True

    def set_globals(self):
        S.DELETE_LOCAL_FILES = bool(int(self.vim.eval('g:floo_delete_local_files')))
        S.SHOW_HIGHLIGHTS = bool(int(self.vim.eval('g:floo_show_highlights')))
        S.SPARSE_MODE = bool(int(self.vim.eval('g:floo_sparse_mode')))
        self.vim.command('let g:floo_connected = 1')

    @neovim.command('FlooJoinWorkspace', sync=True, nargs=1)
    def check_and_join_workspace(self, args):
        if not self.start_ticker():
            return
        workspace_url = args[0]
        self.set_globals()
        try:
            r = api.get_workspace_by_url(workspace_url)
        except Exception as e:
            return editor.error_message('Error joining %s: %s' % (workspace_url, str(e)))
        if r.code >= 400:
            return editor.error_message('Error joining %s: %s' % (workspace_url, r.body))
        msg.debug('Workspace %s exists' % workspace_url)
        return self.join_workspace(workspace_url)

    @neovim.command('FlooRefreshWorkspace')
    @is_connected()
    def refresh_workspace(self):
        S.AGENT.refresh_workspace()

    @neovim.command('FlooSaySomething', sync=True)
    def say_something(self):
        if not S.AGENT:
            return msg.warn('Not connected to a workspace.')
        something = self.vim_input('Say something in %s: ' % (S.AGENT.workspace,), '')
        if something:
            S.AGENT.send_msg(something)

    @neovim.command('FlooShareDirPrivate', sync=True, nargs=1, complete='dir')
    def share_dir_private(self, args):
        if not self.start_ticker():
            return
        dir_to_share = args[0]
        self.set_globals()
        return VUI.share_dir(None, dir_to_share, {'perms': {'AnonymousUser': []}})

    @neovim.command('FlooShareDirPublic', sync=True, nargs=1, complete='dir')
    def share_dir_public(self, args):
        if not self.start_ticker():
            return
        dir_to_share = args[0]
        self.set_globals()
        return VUI.share_dir(None, dir_to_share, {'perms': {'AnonymousUser': ['view_room']}})

    # TODO: figure out optional args. if no arg, should add current buf
    @neovim.command('FlooAddBuf', nargs=1, complete='file')
    @is_connected(True)
    def add_buf(self, args):
        path = args[0] or self.vim.current.buffer.name
        S.AGENT._upload(path)

    @neovim.command('FlooLeaveWorkspace')
    def part_workspace(self):
        VUI.part_workspace()
        self.clear()
        leave_follow_mode()

    # TODO: figure out optional args. if arg, delete that buf
    @neovim.command('FlooDeleteBuf')
    @is_connected(True)
    def delete_buf(self):
        name = self.vim.current.buffer.name
        S.AGENT.delete_buf(name)

    @neovim.command('FlooToggleFollowMode')
    @is_connected()
    def follow(self, follow_mode=None):
        S.FOLLOW_USERS.clear()
        if follow_mode is None:
            follow_mode = not S.FOLLOW_MODE
        S.FOLLOW_MODE = follow_mode
        if follow_mode:
            S.AGENT.highlight()

    @neovim.command('FlooFollowUser')
    @is_connected()
    def follow_user(self):
        S.FOLLOW_MODE = True
        VUI.follow_user(S.AGENT)

    @neovim.command('FlooSummon')
    @is_connected()
    def summon(self):
        self.maybe_selection_changed(ping=True)

    @neovim.command('FlooOpenInBrowser')
    @is_connected(True)
    def open_in_browser(self):
        url = S.AGENT.workspace_url
        # webbrowser can print to stdout, which is hooked up to neovim's msgpack
        # neovim will close the channel on bad msgpack, so squelch all output
        args = [sys.executable, '-m', 'webbrowser', url]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (stdout, stderr) = proc.communicate()
        if stderr:
            msg.error('Error opening browser: %s' % stderr)

    @neovim.command('FlooClearHighlights')
    @is_connected()
    def clear(self):
        buf = S.AGENT.get_buf_by_path(self.vim.current.buffer.name)
        if not buf:
            return
        view = S.AGENT.get_view(buf['id'])
        if view:
            view.clear_all_highlights()

    @neovim.command('FlooToggleHighlights')
    @is_connected()
    def toggle_highlights(self):
        S.SHOW_HIGHLIGHTS = not S.SHOW_HIGHLIGHTS
        if S.SHOW_HIGHLIGHTS:
            self.buf_enter()
            msg.log('Highlights enabled')
            return
        self.clear()
        msg.log('Highlights disabled')

    @neovim.command('FlooCompleteSignup')
    def complete_signup(self):
        if not self.start_ticker():
            return
        msg.debug('Completing signup.')
        if not utils.has_browser():
            msg.log('You need a modern browser to complete the sign up. Go to https://floobits.com to sign up.')
            return
        VUI.pinocchio()

    @neovim.command('FlooUsersInWorkspace', sync=True)
    def users_in_workspace(self):
        if not S.AGENT:
            return msg.warn('Not connected to a workspace.')
        self.vim.command('echom "Users connected to %s"' % (S.AGENT.workspace,))
        for user in list(S.AGENT.workspace_info['users'].values()):
            self.vim.command('echom "  %s connected with %s on %s"' % (user['username'], user['client'], user['platform']))

    @neovim.command('FlooListMessages', sync=True)
    def list_messages(self):
        if not S.AGENT:
            return msg.warn('Not connected to a workspace.')
        self.vim.command('echom "Recent messages for %s"' % (S.AGENT.workspace,))
        for message in S.AGENT.get_messages():
            self.vim.command('echom "  %s"' % (message,))

    @neovim.command('FlooInfo')
    def info(self):
        VUI.info()

    @neovim.autocmd('BufEnter', pattern='*')
    @is_connected()
    def buf_enter(self):
        buf = S.AGENT.get_buf_by_path(self.vim.current.buffer.name)
        if not buf:
            return
        buf_id = buf['id']
        d = S.AGENT.on_load.get(buf_id)
        if d:
            del S.AGENT.on_load[buf_id]
            try:
                d['patch']()
            except Exception as e:
                msg.debug('Error running on_load patch handler for buf %s: %s' % (buf_id, str(e)))
        # NOTE: we call highlight twice in follow mode... thats stupid
        for user_id, highlight in list(S.AGENT.user_highlights.items()):
            if highlight['id'] == buf_id:
                S.AGENT._on_highlight(highlight)

    @neovim.autocmd('CursorMoved', pattern='*')
    @is_connected()
    def cursor_moved(self):
        self.maybe_selection_changed()

    @neovim.autocmd('CursorMovedI', pattern='*')
    @is_connected()
    def cursor_movedi(self):
        self.maybe_selection_changed()

    @neovim.autocmd('TextChanged', pattern='*')
    @is_connected()
    def text_changed(self):
        self.maybe_buffer_changed()

    @neovim.autocmd('TextChangedI', pattern='*')
    @is_connected()
    def text_changedi(self):
        self.maybe_buffer_changed()

    @neovim.autocmd('BufWritePost', pattern='*')
    @is_connected()
    def on_save(self):
        buf = S.AGENT.get_buf_by_path(self.vim.current.buffer.name)
        if buf:
            utils.rate_limit(
                'send_save_%s' % buf['id'],
                250,
                lambda: S.AGENT.send({
                    'name': 'saved',
                    'id': buf['id'],
                })
            )
        else:
            self.maybe_new_file()

    @neovim.autocmd('InsertEnter', pattern='*')
    @is_connected()
    def insert_enter(self):
        if S.FOLLOW_MODE:
            self.vim.command('echom "Leaving follow mode."')
            leave_follow_mode()

    @neovim.autocmd('QuickFixCmdPost', pattern='*')
    @is_connected()
    def quick_fix_cmd_post(self):
        self.maybe_buffer_changed()

    @neovim.autocmd('FileChangedShellPost', pattern='*')
    @is_connected()
    def file_changed_shell_post(self):
        self.maybe_buffer_changed()

    @neovim.autocmd('BufReadPost', sync=True, pattern='*')
    @is_connected()
    def buf_read_post(self):
        self.maybe_new_file()

    @neovim.autocmd('BufWinEnter', sync=True, pattern='*')
    @is_connected()
    def buf_win_enter(self):
        self.maybe_new_file()

    def maybe_new_file(self):
        path = self.vim.current.buffer.name
        if path is None or path == '':
            msg.debug('get:buf buffer has no filename')
            return
        if not os.path.exists(path):
            return
        if not utils.is_shared(path):
            msg.debug('get_buf: %s is not shared' % path)
            return

        buf = S.AGENT.get_buf_by_path(path)
        if not buf:
            if not S.IGNORE:
                msg.warn('S.IGNORE is not set. Uploading anyway.')
                S.AGENT.upload(path)
            if S.IGNORE and not S.IGNORE.is_ignored(path, None, True):
                S.AGENT.upload(path)

    def maybe_buffer_changed(self):
        S.AGENT.maybe_buffer_changed(self.vim.current.buffer)

    def maybe_selection_changed(self, ping=False):
        S.AGENT.maybe_selection_changed(self.vim.current.buffer, ping)

    def join_workspace(self, workspace_url, d='', upload_path=None):
        editor.line_endings = self._get_line_endings()
        cwd = self.vim.eval('getcwd()')
        if cwd:
            cwd = [cwd]
        else:
            cwd = []
        VUI.join_workspace_by_url(None, workspace_url, cwd)
        self.vim.command(":cd %s" % S.PROJECT_PATH)

    def vim_input(self, prompt, default, completion=None):
        self.vim.command('call inputsave()')
        if completion:
            cmd = "let user_input = input('%s', '%s', '%s')" % (prompt, default, completion)
        else:
            cmd = "let user_input = input('%s', '%s')" % (prompt, default)
        self.vim.command(cmd)
        self.vim.command('call inputrestore()')
        return self.vim.eval('user_input')

    def _get_line_endings(self):
        formats = self.vim.eval('&fileformats')
        if not formats:
            return '\n'
        name = formats.split(',')[0]
        if name == 'dos':
            return '\r\n'
        return '\n'

    def vim_choice(self, prompt, default, choices):
        default = choices.index(default) + 1
        choices_str = '\n'.join(['&%s' % choice for choice in choices])
        try:
            choice = int(self.vim.eval('confirm("%s", "%s", %s)' % (prompt, choices_str, default)))
        except KeyboardInterrupt:
            return None
        if choice == 0:
            return None
        return choices[choice - 1]
