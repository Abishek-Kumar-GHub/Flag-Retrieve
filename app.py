import os
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'fallback_secret_key')

# ── Flags ────────────────────────────────────────────────────────────────────
FLAG1 = os.getenv('FLAG1')
FLAG2 = os.getenv('FLAG2')
FLAG3 = os.getenv('FLAG3')
FLAG4 = os.getenv('FLAG4')

# ── Admin credentials ─────────────────────────────────────────────────────────
POINTS_ADMIN_USER = os.getenv('POINTS_USER', 'pointsmanage')
POINTS_ADMIN_PASS = os.getenv('POINTS_PASS', 'poi123')

# ── XML storage ───────────────────────────────────────────────────────────────
DATA_FILE = 'ctf_data.xml'
_lock = threading.Lock()          # guards all XML reads + writes


# ═══════════════════════════════════════════════════════════════════════════════
#  XML HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _indent(elem: ET.Element, level: int = 0) -> None:
    """Add pretty-print indentation (works on Python < 3.9 too)."""
    pad = "\n" + "    " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = pad + "    "
        if not elem.tail or not elem.tail.strip():
            elem.tail = pad
        for child in elem:
            _indent(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = pad
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = pad
    if not level:
        elem.tail = "\n"


def _init_xml() -> None:
    """Create the XML file with an empty <participants> block if absent."""
    if not os.path.exists(DATA_FILE):
        root = ET.Element('ctf_data')
        ET.SubElement(root, 'participants')
        _write_root(root)


def _read_root() -> ET.Element:
    """Parse and return the root element."""
    _init_xml()
    return ET.parse(DATA_FILE).getroot()


def _write_root(root: ET.Element) -> None:
    """Indent and write the root element to disk."""
    _indent(root)
    tree = ET.ElementTree(root)
    tree.write(DATA_FILE, encoding='utf-8', xml_declaration=True)


# ── Public API ────────────────────────────────────────────────────────────────

def load_participants() -> dict:
    """
    Return all participants as a dict keyed by GitHub username.

    Shape of each value:
        {
            'name':         str,
            'points':       int,
            'flags_found':  set[int],
            'completed_at': datetime | None,
            'registered_at': datetime,
        }
    """
    with _lock:
        root = _read_root()

    participants: dict = {}
    for p in root.find('participants').findall('participant'):
        username = p.findtext('username', '').strip()
        if not username:
            continue

        # flags_found stored as "1,2,3"
        raw_flags = p.findtext('flags_found', '').strip()
        flags_found: set[int] = set()
        if raw_flags:
            for tok in raw_flags.split(','):
                tok = tok.strip()
                if tok.isdigit():
                    flags_found.add(int(tok))

        def _parse_dt(text: str) -> datetime | None:
            text = (text or '').strip()
            if not text:
                return None
            try:
                return datetime.fromisoformat(text)
            except ValueError:
                return None

        participants[username] = {
            'name':          p.findtext('name', username),
            'points':        int(p.findtext('points', '0') or '0'),
            'flags_found':   flags_found,
            'completed_at':  _parse_dt(p.findtext('completed_at', '')),
            'registered_at': _parse_dt(p.findtext('registered_at', '')) or datetime.now(),
        }
    return participants


def save_participants(participants: dict) -> None:
    """Serialise the participants dict and overwrite the XML file."""
    root = ET.Element('ctf_data')
    participants_el = ET.SubElement(root, 'participants')

    for username, data in participants.items():
        p = ET.SubElement(participants_el, 'participant')
        ET.SubElement(p, 'username').text      = username
        ET.SubElement(p, 'name').text          = data['name']
        ET.SubElement(p, 'points').text        = str(data['points'])
        ET.SubElement(p, 'flags_found').text   = ','.join(
            str(f) for f in sorted(data['flags_found'])
        )
        ET.SubElement(p, 'completed_at').text  = (
            data['completed_at'].isoformat() if data['completed_at'] else ''
        )
        ET.SubElement(p, 'registered_at').text = (
            data['registered_at'].isoformat() if data['registered_at'] else ''
        )

    with _lock:
        _write_root(root)


def upsert_participant(username: str, name: str) -> None:
    """Register a new user; leave existing users untouched."""
    participants = load_participants()
    if username not in participants:
        participants[username] = {
            'name':          name,
            'points':        0,
            'flags_found':   set(),
            'completed_at':  None,
            'registered_at': datetime.now(),
        }
        save_participants(participants)


def award_flag(username: str, flag_num: int) -> dict:
    """
    Try to award points for flag_num to username.

    Returns:
        {'points_awarded': bool, 'already_found': bool}
    """
    participants = load_participants()
    if username not in participants:
        return {'points_awarded': False, 'already_found': False}

    participant = participants[username]
    if flag_num in participant['flags_found']:
        return {'points_awarded': False, 'already_found': True}

    participant['flags_found'].add(flag_num)
    participant['points'] += 100
    if len(participant['flags_found']) == 4 and participant['completed_at'] is None:
        participant['completed_at'] = datetime.now()

    save_participants(participants)
    return {'points_awarded': True, 'already_found': False}


def get_found_flags(username: str) -> set:
    """Return the set of flag numbers found by username."""
    participants = load_participants()
    return participants.get(username, {}).get('flags_found', set())


def reset_all() -> None:
    """Wipe every participant from the XML store."""
    save_participants({})


# ═══════════════════════════════════════════════════════════════════════════════
#  OAUTH / GITHUB
# ═══════════════════════════════════════════════════════════════════════════════

oauth = OAuth(app)
github = oauth.register(
    name='github',
    client_id=os.getenv('GITHUB_CLIENT_ID'),
    client_secret=os.getenv('GITHUB_CLIENT_SECRET'),
    access_token_url='https://github.com/login/oauth/access_token',
    authorize_url='https://github.com/login/oauth/authorize',
    api_base_url='https://api.github.com/',
    client_kwargs={'scope': 'user:email'},
)


# ═══════════════════════════════════════════════════════════════════════════════
#  CONTEXT PROCESSOR
# ═══════════════════════════════════════════════════════════════════════════════

@app.context_processor
def inject_login_status():
    return {
        'show_login':    'ctf_username' not in session,
        'ctf_username':  session.get('ctf_username', ''),
        'ctf_name':      session.get('ctf_name', ''),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES — AUTH
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/auth/github')
def github_login():
    redirect_uri = url_for('github_authorize', _external=True)
    return github.authorize_redirect(redirect_uri)


@app.route('/auth/github/callback')
def github_authorize():
    token = github.authorize_access_token()
    resp  = github.get('user', token=token)
    user  = resp.json()

    username = user.get('login')
    name     = user.get('name') or username

    session['ctf_username'] = username
    session['ctf_name']     = name

    # Persist to XML (no-op if user already exists)
    upsert_participant(username, name)

    return redirect(url_for('index'))


@app.route('/ctf-logout')
def ctf_logout():
    session.pop('ctf_username', None)
    session.pop('ctf_name',     None)
    return redirect(url_for('index'))


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES — FLAG SUBMISSION
# ═══════════════════════════════════════════════════════════════════════════════

_FLAG_META = {
    FLAG1: (1, 'Path Traversal & Privilege Escalation',
            'This flag was hidden in a sensitive system file accessible via directory traversal. '
            'By enumerating paths and exploiting improper file access controls, '
            'you discovered the flag in /etc/passwd.'),
    FLAG2: (2, 'Privilege Escalation + Steganography',
            'This flag required finding hardcoded credentials in the application source code, '
            'escalating privileges to access restricted reports, and then extracting hidden data '
            'from files using steganography techniques.'),
    FLAG3: (3, 'Network Packet Capture Analysis',
            'This flag was embedded within network traffic. By analyzing the packet capture file '
            'with appropriate tools, you were able to extract the hidden flag from the captured data.'),
    FLAG4: (4, 'PostgreSQL Database Exploitation',
            'This flag was obtained by identifying a misconfigured public function in a PostgreSQL '
            'database. By connecting as a limited user and exploiting the function\'s elevated '
            'permissions, you were able to retrieve sensitive data from a restricted table.'),
}


@app.route('/', methods=['GET', 'POST'])
def index():
    result   = None
    username = session.get('ctf_username')

    if request.method == 'POST':
        flag_input = request.form.get('flag', '').strip()

        if flag_input and flag_input in _FLAG_META:
            flag_num, category, description = _FLAG_META[flag_input]
            result = {
                'correct':     True,
                'flag_num':    flag_num,
                'category':    category,
                'description': description,
                'input':       flag_input,
            }

            if username:
                outcome = award_flag(username, flag_num)
                result.update(outcome)

        elif flag_input:
            result = {'correct': False, 'input': flag_input}

    found_flags = get_found_flags(username) if username else set()
    return render_template('index.html', result=result, found_flags=found_flags)


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES — LEADERBOARD
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/points')
def points_leaderboard():
    participants = load_participants()

    leaderboard = [
        {
            'username':     uname,
            'name':         data['name'],
            'points':       data['points'],
            'flags_found':  len(data['flags_found']),
            'completed_at': data['completed_at'],
            'registered_at': data['registered_at'],
        }
        for uname, data in participants.items()
    ]

    leaderboard.sort(key=lambda e: (
        -e['points'],
        e['completed_at'] if e['completed_at'] else datetime.max,
    ))

    return render_template('points.html', leaderboard=leaderboard)


# ═══════════════════════════════════════════════════════════════════════════════
#  ROUTES — POINTS RESET (admin)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/points/reset', methods=['GET', 'POST'])
def points_reset():
    error         = None
    success       = False
    authenticated = session.get('points_admin_logged_in', False)

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'login':
            u = request.form.get('username', '').strip()
            p = request.form.get('password', '').strip()
            if u == POINTS_ADMIN_USER and p == POINTS_ADMIN_PASS:
                session['points_admin_logged_in'] = True
                authenticated = True
            else:
                error = 'Invalid credentials.'

        elif action == 'reset' and authenticated:
            reset_all()
            success = True

    return render_template('points_reset.html',
                           error=error,
                           success=success,
                           authenticated=authenticated)


# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    _init_xml()          # create ctf_data.xml on first run
    app.run(debug=True, host='0.0.0.0', port=5000)