"""Bounded fallback for the reviewed native WeChat data-authorization dialog.

Only the exact MPGdprPolicyUI dialog is eligible. Preserve all cached packages,
try its checkbox and Confirm once, then preserve again before allowing the
caller to close the app. A known but unsuccessful consent falls back to closing;
unknown native screens, verification and rate limits remain blocking.

Receipts and bytes stay under the caller's output directory. This module never
assigns package ownership, upgrades acquisition status, or changes a task queue.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

from . import artifacts

PRIVACY_ACTIVITY = 'com.tencent.mm.model.gdpr.ui.MPGdprPolicyUI'
PRIVACY_TITLE = 'Authorize Data: WeChat Login'
PRIVACY_AGREEMENT = 'I have read and agree to the above conditions'
CACHE = artifacts.CACHE_ROOT + '/'


class NativePrivacyError(RuntimeError):
    """The reviewed privacy fallback could not prove a safe close boundary."""


class CacheInventoryChanged(NativePrivacyError):
    """A complete preservation pass observed more or different remote bytes."""

    def __init__(self, receipt):
        super().__init__('Cache inventory changed during preservation; close refused')
        self.receipt = receipt


class NativePrivacyRateLimit(NativePrivacyError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class NativePrivacySecurity(NativePrivacyError):
    """WeChat shows account/security verification or a blocking challenge."""


class PrivacyControlUnavailable(ValueError):
    """Known dialog, but native control state cannot be operated reliably."""


class IncompletePrivacyScreen(NativePrivacyError):
    """Exact GDPR title/agreement are visible, but Confirm is obscured."""


class LoadingPrivacyScreen(NativePrivacyError):
    """Only the reviewed native GDPR loading header is visible."""


class TransientNativeFocus(NativePrivacyError):
    """A known WeChat activity temporarily has no focused window."""


def privacy_loading_shell(screen):
    """Recognize an empty GDPR header solely to wait for fresh evidence."""
    try:
        width, height = screen['width'], screen['height']
        if width <= 0 or height <= 0:
            return False
        header = []
        for node in screen['nodes']:
            text = node.get('text', '').strip()
            if not text:
                continue
            left, top, right, bottom = node['bounds']
            if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                return False
            if bottom <= height * .06:
                continue  # Clock/status bar; security checks have already run.
            if (node.get('package', 'com.tencent.mm') != 'com.tencent.mm'
                    or node.get('confidence', 0) < .8
                    or not height * .07 <= top < bottom <= height * .14):
                return False
            if text == 'X' and right <= width * .15:
                header.append(text)
            elif text == 'WeChat' and width * .3 <= left < right <= width * .7:
                header.append(text)
            else:
                return False
        return sorted(header) == ['WeChat', 'X']
    except (KeyError, TypeError, ValueError):
        return False


def native_identities(focus):
    current = [line.strip() for line in focus.splitlines() if 'mCurrentFocus=' in line]
    focused = [line for line in focus.splitlines() if 'mFocusedApp=' in line]
    if current == ['mCurrentFocus=null'] and len(focused) == 1:
        match = re.search(r'com\.tencent\.mm/(\.?[A-Za-z0-9_.$]+)', focused[0])
        if match:
            activity = match[1]
            if activity.startswith('.'):
                activity = 'com.tencent.mm' + activity
            if (activity == PRIVACY_ACTIVITY or re.fullmatch(
                    r'com\.tencent\.mm\.plugin\.appbrand\.ui\.AppBrandUI\d*', activity)):
                raise TransientNativeFocus('Known WeChat activity has no focused window yet')
    identities = []
    for field in ('mCurrentFocus', 'mFocusedApp'):
        lines = [line for line in focus.splitlines() if field + '=' in line]
        if len(lines) != 1: raise NativePrivacyError('Unknown native focus')
        match = re.search(r'com\.tencent\.mm/(\.?[A-Za-z0-9_.$]+)', lines[0])
        if not match: raise NativePrivacyError('Unexpected native package')
        activity = match[1]
        if activity.startswith('.'): activity = 'com.tencent.mm' + activity
        identities.append(activity)
    if identities[0] != identities[1]: raise NativePrivacyError('Native focus identities differ')
    return tuple(identities)


def check_security_walls(screen):
    text = '\n'.join(n['text'] for n in screen.get('nodes', []))
    if re.search(r'too (?:many|frequent) (?:requests|attempts)|rate limit(?:ed| exceeded)?|操作(?:过于|太)?频繁|访问(?:过于|太)?频繁|请求过于频繁', text, re.I):
        delay = re.search(r'(\d+)\s*(seconds?|minutes?|秒|分钟)', text, re.I)
        seconds = int(delay[1]) * (60 if delay[2].lower().startswith(('minute', '分钟')) else 1) if delay else 0
        raise NativePrivacyRateLimit('WeChat reports a request limit', seconds)
    if re.search(r'captcha|security verification|安全验证|帐号被封|账号被封', text, re.I):
        raise NativePrivacySecurity('WeChat requires security verification')


def _check_native_activities(identities):
    for activity in identities:
        if not (activity in (PRIVACY_ACTIVITY, 'com.tencent.mm.ui.LauncherUI',
                'com.tencent.mm.plugin.webview.ui.tools.fts.MMFTSSearchTabWebViewUI',
                'com.tencent.mm.plugin.appbrand.ui.AppBrandPluginUI') or
                re.fullmatch(r'com\.tencent\.mm\.plugin\.appbrand\.ui\.AppBrandUI\d*', activity)):
            raise NativePrivacyError('Unexpected native activity: ' + activity)


def classify_focus(focus, screen):
    identities = native_identities(focus)
    check_security_walls(screen)
    if identities == (PRIVACY_ACTIVITY,) * 2:
        if privacy_loading_shell(screen):
            raise LoadingPrivacyScreen('Native privacy dialog is still loading')
        texts = {n['text'].strip() for n in screen.get('nodes', [])
                 if n.get('package', 'com.tencent.mm') == 'com.tencent.mm'
                 and n.get('confidence', 0) >= .8}
        if {PRIVACY_TITLE, PRIVACY_AGREEMENT} <= texts and 'Confirm' not in texts:
            raise IncompletePrivacyScreen('Native privacy Confirm is not visible')
        if not {PRIVACY_TITLE, PRIVACY_AGREEMENT, 'Confirm'} <= texts:
            raise NativePrivacyError('Unrecognized native privacy dialog')
        return identities
    _check_native_activities(identities)
    return identities


def reviewed_observation(observe, label, *, wait=None, check_stop=lambda: None):
    """Reobserve loading UI or missing known native focus at most three times.

    Waiting never authorizes a tap or close. A temporarily missing window can
    only be retried for a known WeChat activity. Security checks run before any
    retry, and both native identities and the complete screen classification
    must agree on fresh evidence before an action is admitted.
    """
    wait = time.sleep if wait is None else wait
    for attempt in range(3):
        check_stop()
        observation = observe(label if attempt == 0 else label + '-retry-' + str(attempt))
        check_stop()
        try:
            return observation, classify_focus(*observation)
        except (IncompletePrivacyScreen, LoadingPrivacyScreen, TransientNativeFocus):
            check_security_walls(observation[1])
            if attempt == 2:
                raise
            check_stop()
            wait(2)
            check_stop()


def _parse_hierarchy(xml):
    start = xml.index('<hierarchy')
    end = xml.find('</hierarchy>',start)
    if end >= 0:
        document = xml[start:end + len('</hierarchy>')]
    else:
        empty = re.match(r'<hierarchy\b[^>]*?/\s*>',xml[start:])
        if not empty: raise ValueError('Hierarchy closing tag unavailable')
        document = empty[0]
    root = ET.fromstring(document)
    if root.tag != 'hierarchy': raise ValueError('Unexpected hierarchy root')
    return root


def screen_from_xml(xml, size):
    try:
        tree = _parse_hierarchy(xml)
    except (AttributeError, TypeError, ValueError, ET.ParseError) as error:
        raise NativePrivacyError('Native accessibility hierarchy is unreadable') from error
    nodes = []
    for node in tree.iter():
        match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', ''))
        for text in dict.fromkeys((node.get('text', '').strip(), node.get('content-desc', '').strip())):
            if not text:
                continue
            observed = {'text': text, 'confidence': 1, 'package': node.get('package', '')}
            if match:
                observed['bounds'] = list(map(int, match.groups()))
            nodes.append(observed)
    return {'width': size[0], 'height': size[1], 'nodes': nodes}


def privacy_controls(xml, screen):
    """Use native checked/enabled state, never infer the checkbox from OCR."""
    import xml.etree.ElementTree as ET
    try:
        root = _parse_hierarchy(xml)
        parents = {child: parent for parent in root.iter() for child in parent}
        width, height = screen['width'], screen['height']
        def bounds(node):
            match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', ''))
            if not match: raise PrivacyControlUnavailable('Missing control bounds')
            box = tuple(map(int, match.groups()))
            if not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
                raise PrivacyControlUnavailable('Control outside screen')
            return box
        def named(text):
            matches = [n for n in root.iter()
                       if n.get('package') == 'com.tencent.mm'
                       and (n.get('text', '').strip() == text or n.get('content-desc', '').strip() == text)]
            if len(matches) != 1: raise PrivacyControlUnavailable('Ambiguous privacy control: ' + text)
            bounds(matches[0])
            return matches[0]
        named(PRIVACY_TITLE)
        label = named(PRIVACY_AGREEMENT)
        label_box = bounds(label)
        checkboxes = []
        for node in root.iter():
            if node.get('package') != 'com.tencent.mm' or node.get('checkable') != 'true': continue
            box = bounds(node)
            middle = (box[1] + box[3]) / 2
            if node is label or label_box[1] <= middle <= label_box[3]: checkboxes.append(node)
        if len(checkboxes) != 1: raise PrivacyControlUnavailable('Ambiguous privacy checkbox')
        checkbox = checkboxes[0]
        if checkbox.get('checked') not in ('true', 'false') or checkbox.get('enabled') != 'true':
            raise PrivacyControlUnavailable('Checkbox state unavailable')
        if checkbox.get('clickable') != 'true': raise PrivacyControlUnavailable('Checkbox is not clickable')
        confirm_label = named('Confirm')
        confirm = confirm_label
        # Some WeChat builds expose a label inside the native clickable button.
        for _ in range(2):
            if confirm.get('clickable') == 'true': break
            confirm = parents.get(confirm)
            if confirm is None: raise PrivacyControlUnavailable('Confirm is not clickable')
        if confirm.get('clickable') != 'true' or confirm.get('package') != 'com.tencent.mm':
            raise PrivacyControlUnavailable('Confirm is not clickable')
        button_box = bounds(confirm)
        if button_box[1] < height * .7 or button_box[3] - button_box[1] > height * .2:
            raise PrivacyControlUnavailable('Unexpected Confirm geometry')
        def center(node):
            left, top, right, bottom = bounds(node)
            return ((left + right) // 2, (top + bottom) // 2)
        return {'checked': checkbox.get('checked') == 'true',
                'checkbox': center(checkbox), 'confirm': center(confirm_label),
                'confirm_enabled': confirm.get('enabled') == 'true'}
    except (KeyError, TypeError, ET.ParseError, ValueError) as error:
        if isinstance(error, PrivacyControlUnavailable): raise
        raise PrivacyControlUnavailable('Unreadable privacy controls: ' + str(error)) from error


def resolve_privacy_once(observe, hierarchy, tap, wait, check_stop):
    """One consent attempt; failed consent falls back to a reviewed close only.

    Caller must preserve all bytes before entering and again after returning.
    Every returned observation is independently admitted by classify_focus.
    Unknown activities, changed identities and security/rate walls still raise.
    """
    import subprocess
    evidence = {'attempted': False, 'checkbox_tapped': False, 'confirm_tapped': False}
    def checked_observation(label):
        return reviewed_observation(observe, label, wait=wait, check_stop=check_stop)
    def fresh_controls(label):
        observation, identities = checked_observation(label)
        if identities != (PRIVACY_ACTIVITY,) * 2:
            return observation, None
        xml = None
        def observe_controls(current_label):
            nonlocal xml
            # A retry may outlive the checkbox state first read. Never use
            # coordinates or checked/enabled state retained across that wait.
            xml = hierarchy(current_label)
            return observe(current_label + '-fresh')
        # Reclassify after accessibility reads. A valid ordinary app may have
        # replaced the dialog already; in that case do not tap stale controls.
        observation, identities = reviewed_observation(
            observe_controls, label, wait=wait, check_stop=check_stop)
        if identities != (PRIVACY_ACTIVITY,) * 2:
            return observation, None
        return observation, privacy_controls(xml, observation[1])
    try:
        observation, controls = fresh_controls('privacy-controls')
        if controls is None:
            return observation, dict(evidence, outcome='already_left_dialog')
        if not controls['checked']:
            check_stop()
            evidence['attempted'] = evidence['checkbox_tapped'] = True
            tap(controls['checkbox'])
            wait(.5)
            observation, controls = fresh_controls('privacy-checked')
            if controls is None:
                return observation, dict(evidence, outcome='left_after_checkbox')
            if not controls['checked']:
                raise PrivacyControlUnavailable('Checkbox did not become checked')
        if not controls['confirm_enabled']:
            raise PrivacyControlUnavailable('Confirm is disabled')
        check_stop()
        evidence['attempted'] = evidence['confirm_tapped'] = True
        tap(controls['confirm'])
        for attempt in range(3):
            wait(1)
            check_stop()
            observation, identities = checked_observation('privacy-confirmed-' + str(attempt))
            if identities != (PRIVACY_ACTIVITY,) * 2:
                return observation, dict(evidence, outcome='confirmed')
        return observation, dict(evidence, outcome='close_only', reason='Privacy dialog remained or returned')
    except (PrivacyControlUnavailable, OSError, subprocess.SubprocessError) as error:
        # A missing/failed control is not a reason to accept unknown screens.
        # Re-observation can still fail closed on security or native boundaries.
        check_stop()
        observation, _ = checked_observation('privacy-fallback')
        return observation, dict(evidence, outcome='close_only', reason=str(error))


def preserve_after_privacy(observed, consent, preserve_cache, observe, *,
                           wait=None, check_stop=lambda: None):
    """Preserve new bytes and allow one late return of the reviewed dialog."""
    expected = classify_focus(*observed)
    result = preserve_cache()
    _, actual = reviewed_observation(observe, 'after-privacy-preservation',
                                     wait=wait, check_stop=check_stop)
    if actual != expected:
        if actual != (PRIVACY_ACTIVITY,) * 2:
            raise NativePrivacyError('Native focus changed after privacy preservation')
        consent = dict(consent, outcome='close_only',
                       reason='Privacy dialog returned during preservation')
        # The known dialog can return after successful confirmation. Do not
        # confirm twice: take one final stable preservation before closing it.
        result = preserve_cache()
        _, final = reviewed_observation(observe, 'after-returned-privacy-preservation',
                                        wait=wait, check_stop=check_stop)
        if final != actual:
            raise NativePrivacyError('Native focus kept changing after privacy fallback')
    return result, consent


def preserve(serial, root, inventory, pull):
    root = Path(root).absolute()
    if root.resolve() != root:
        raise NativePrivacyError('Unsafe preservation directory')
    blobs = root / 'blobs'
    blobs.mkdir(parents=True, exist_ok=True)
    if blobs.is_symlink():
        raise NativePrivacyError('Unsafe blob directory')
    before = inventory()
    entries = []
    for remote, digest in sorted(before.items()):
        if (not remote.startswith(CACHE) or '..' in Path(remote).parts or
                not remote.endswith('.wxapkg') or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise NativePrivacyError('Unsafe cache inventory')
        local = blobs / (digest + '.wxapkg')
        if local.is_symlink():
            raise NativePrivacyError('Unsafe blob symlink')
        if not local.exists():
            content = pull(remote)
            if not content or hashlib.sha256(content).hexdigest() != digest:
                raise NativePrivacyError('Pulled SHA mismatch')
            temp = blobs / ('.pending-' + uuid.uuid4().hex)
            try:
                with temp.open('xb') as stream:
                    stream.write(content); stream.flush(); os.fsync(stream.fileno())
                os.link(temp, local)
            finally:
                temp.unlink(missing_ok=True)
        descriptor = os.open(local, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            a = os.fstat(stream.fileno())
            if not stat.S_ISREG(a.st_mode) or a.st_nlink != 1:
                raise NativePrivacyError('Unsafe shared blob')
            h = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b''): h.update(chunk)
            b = os.fstat(stream.fileno())
        if h.hexdigest() != digest or (a.st_ino,a.st_size,a.st_mtime_ns) != (b.st_ino,b.st_size,b.st_mtime_ns):
            raise NativePrivacyError('Retained SHA mismatch or changed bytes')
        entries.append({'remote_path': remote, 'sha256': digest, 'bytes': b.st_size, 'local_path': str(local)})
    after = inventory()
    receipt = {'serial': serial, 'at': datetime.now(timezone.utc).isoformat(),
               'inventory_before': before, 'inventory_after': after,
               'packages': entries, 'attribution': 'UNASSIGNED', 'complete': before == after}
    path = root / ('preservation-' + uuid.uuid4().hex + '.json')
    with path.open('x') as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2); stream.flush(); os.fsync(stream.fileno())
    if before != after:
        raise CacheInventoryChanged(receipt)
    return receipt


def preserve_stable(serial, root, inventory, pull, check_stop=lambda: None, wait=None):
    """Allow at most three passes when downloads only add cached packages.

    Every pass retains its receipt and verifies all cached bytes. Removed or
    replaced files, failed hashes and unreadable inventories remain blocking.
    A later pass must also retain the hashes seen at the end of the prior pass.
    """
    wait = time.sleep if wait is None else wait
    expected = {}
    for attempt in range(3):
        check_stop()
        first_inventory = True
        def checked_inventory():
            nonlocal first_inventory
            check_stop()
            current = inventory()
            if first_inventory and any(current.get(path) != digest for path, digest in expected.items()):
                raise NativePrivacyError('Cache files disappeared or changed between preservation passes')
            first_inventory = False
            return current
        try:
            return preserve(serial, root, checked_inventory, pull)
        except CacheInventoryChanged as error:
            before = error.receipt['inventory_before']
            after = error.receipt['inventory_after']
            additions_only = (len(after) > len(before)
                              and all(after.get(path) == digest for path, digest in before.items()))
            if not additions_only or attempt == 2:
                raise
            expected = after
            check_stop()
            wait(1)


def before_close(serial, output_root, check_stop=lambda: None, adb='adb', hierarchy_source=None):
    """Handle this native dialog before the caller's existing close action.

    Return None without UI/cache changes when the foreground is unrelated.
    Return a receipt for accepted or close-only fallback. The caller may then
    terminate WeChat and advance using its existing queue semantics. Exceptions
    must prevent termination. ``hierarchy_source`` is an optional zero-argument
    XML provider; Appium callers should supply their active session's source so
    a second UiAutomator instrumentation process is never launched.
    """
    try:
        return _before_close(serial, output_root, check_stop, adb, hierarchy_source)
    except NativePrivacyError:
        raise
    except (OSError, subprocess.SubprocessError, artifacts.ArtifactError, ValueError, TypeError) as error:
        raise NativePrivacyError('Native privacy fallback failed: ' + str(error)) from error


def _before_close(serial, output_root, check_stop, adb, hierarchy_source):
    if not isinstance(serial, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]+', serial):
        raise NativePrivacyError('Invalid device serial')
    def command(*args, text=True):
        return subprocess.check_output([adb, '-s', serial, *args],text=text,timeout=30)
    check_stop()
    first = command('shell','dumpsys','window')
    relevant = '\n'.join(line for line in first.splitlines()
                         if 'mCurrentFocus=' in line or 'mFocusedApp=' in line)
    if not re.search(r'com\.tencent\.mm/(?:com\.tencent\.mm)?\.model\.gdpr\.ui\.MPGdprPolicyUI\b', relevant):
        return None
    try:
        if native_identities(first) != (PRIVACY_ACTIVITY,) * 2:
            raise NativePrivacyError('Native privacy focus mismatch')
    except TransientNativeFocus:
        # The initial probe only detects whether the privacy fallback applies.
        # A known GDPR activity without a window must enter the same bounded
        # focus/XML/focus observations as later transitions; it authorizes no UI.
        pass
    root = Path(output_root).absolute() / 'native_privacy' / serial
    if root.resolve() != root: raise NativePrivacyError('Unsafe privacy output directory')
    root.mkdir(parents=True,exist_ok=True)
    evidence = root / ('boundary-' + uuid.uuid4().hex)
    evidence.mkdir()
    sizes = re.findall(r'(?:Physical|Override) size:\s*(\d+)x(\d+)', command('shell','wm','size'))
    if not sizes: raise NativePrivacyError('Device dimensions unavailable')
    size = tuple(map(int,sizes[-1]))
    if not all(0 < n < 20000 for n in size): raise NativePrivacyError('Invalid device dimensions')
    def read_hierarchy():
        if hierarchy_source is None: return command('shell','uiautomator','dump','/dev/tty')
        try:
            xml = hierarchy_source()
        except Exception as error:
            raise NativePrivacyError('Active session hierarchy unavailable') from error
        if not isinstance(xml,str): raise NativePrivacyError('Active session hierarchy is not XML')
        return xml
    def observe(label):
        check_stop()
        for retry in range(2):
            focus = command('shell','dumpsys','window')
            try:
                identities = native_identities(focus)
                _check_native_activities(identities)
            except TransientNativeFocus:
                identities = None
            xml = read_hierarchy()
            current = command('shell','dumpsys','window')
            try:
                current_identities = native_identities(current)
                _check_native_activities(current_identities)
            except TransientNativeFocus:
                current_identities = None
            screen = screen_from_xml(xml,size)
            transient = identities is None or current_identities is None
            if not transient and identities != current_identities:
                # One bounded re-observation handles a legitimate transition
                # after Confirm. Never act on the old screen's coordinates.
                if retry == 0: continue
                raise NativePrivacyError('Native focus changed during observation')
            policy = command('shell','dumpsys','window','policy')
            if not re.search(r'(?:showing|isKeyguardShowing|mShowingLockscreen)=false',policy):
                raise NativePrivacyError('Unlocked state not proven')
            # Return the missing focus together with the captured screen so
            # reviewed_observation can check security before waiting. Discard
            # its XML after the wait, even if the second focus already settled.
            observed_focus = focus if identities is None else current
            (evidence / (label+'-focus.txt')).write_text(observed_focus)
            (evidence / (label+'-ui.xml')).write_text(xml)
            return observed_focus,screen
    def hierarchy(label):
        check_stop()
        xml = read_hierarchy()
        (evidence / (label+'-controls.xml')).write_text(xml)
        return xml
    def tap(point):
        check_stop()
        if native_identities(command('shell','dumpsys','window')) != (PRIVACY_ACTIVITY,) * 2:
            raise NativePrivacyError('Native privacy focus changed before tap')
        subprocess.run([adb,'-s',serial,'shell','input','tap',str(point[0]),str(point[1])],
                       check=True,timeout=30)
    def preserve_cache():
        check_stop()
        return preserve_stable(serial,root,lambda: artifacts.inventory(serial,adb),
                               lambda path: artifacts._read_device(serial,'cat -- '+shlex.quote(path),adb),
                               check_stop=check_stop)
    _, initial = reviewed_observation(observe, 'before', check_stop=check_stop)
    result = preserve_cache()
    after, after_identity = reviewed_observation(observe, 'after-preservation', check_stop=check_stop)
    if after_identity != initial:
        # The dialog may have closed itself. A safe freshly observed app needs
        # no consent action, but any newly loaded bytes still need preservation.
        consent = {'attempted':False,'checkbox_tapped':False,'confirm_tapped':False,
                   'outcome':'already_left_dialog'}
        observed = after
    else:
        observed,consent = resolve_privacy_once(observe,hierarchy,tap,time.sleep,check_stop)
    (evidence/'privacy-fallback.json').write_text(json.dumps(consent,ensure_ascii=False,indent=2))
    result,consent = preserve_after_privacy(observed,consent,preserve_cache,observe,
                                          check_stop=check_stop)
    check_stop()
    receipt = dict(consent,serial=serial,at=datetime.now(timezone.utc).isoformat(),
                   complete=True,packages=len(result['packages']),evidence=str(evidence))
    (evidence/'privacy-fallback.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
    (evidence/'CLOSE_AUTHORIZED.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
    return receipt
