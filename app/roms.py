"""Retro ROM library: scanning, browser play via EmulatorJS, per-user save states.

Kept separate from the Switch pipeline (Files/Titles/Apps, watcher, tasks) so the fork
stays easy to rebase on upstream Ownfoil. ROMs live under ROMS_ROOT, one folder per
platform: /roms/gba/Game.gba, /roms/snes/Game.sfc, /roms/psx/Game.chd ...
"""
import datetime
import logging
import os
import threading

from flask import Blueprint, abort, jsonify, render_template, request, send_file
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from auth import access_required, admin_account_created
from constants import CONFIG_DIR
from db import db

logger = logging.getLogger('main')

ROMS_ROOT = os.environ.get('OWNFOIL_ROMS_DIR', '/roms')
STATES_DIR = os.path.join(CONFIG_DIR, 'rom_states')
MAX_STATE_BYTES = 64 * 1024 * 1024
EMULATORJS_DATA = os.environ.get('OWNFOIL_EMULATORJS_CDN', 'https://cdn.emulatorjs.org/stable/data/')

# folder key -> (display name, EmulatorJS core, extensions)
ARCHIVES = ['zip', '7z']
PLATFORMS = {
    'gba':          ('Game Boy Advance', 'gba',       ['gba'] + ARCHIVES),
    'gb':           ('Game Boy',         'gb',        ['gb'] + ARCHIVES),
    'gbc':          ('Game Boy Color',   'gb',        ['gbc', 'gb'] + ARCHIVES),
    'nes':          ('NES',              'nes',       ['nes'] + ARCHIVES),
    'snes':         ('SNES',             'snes',      ['sfc', 'smc'] + ARCHIVES),
    'n64':          ('Nintendo 64',      'n64',       ['n64', 'z64', 'v64'] + ARCHIVES),
    'nds':          ('Nintendo DS',      'nds',       ['nds'] + ARCHIVES),
    'megadrive':    ('Mega Drive',       'segaMD',    ['md', 'gen', 'smd', 'bin'] + ARCHIVES),
    'mastersystem': ('Master System',    'segaMS',    ['sms'] + ARCHIVES),
    'gamegear':     ('Game Gear',        'segaGG',    ['gg'] + ARCHIVES),
    # Single-file disc formats only: EmulatorJS loads one URL, so .cue+.bin pairs won't work.
    'psx':          ('PlayStation',      'psx',       ['chd', 'pbp']),
    'atari2600':    ('Atari 2600',       'atari2600', ['a26', 'bin'] + ARCHIVES),
}
FOLDER_ALIASES = {
    'gameboy': 'gb', 'gameboycolor': 'gbc', 'gameboyadvance': 'gba',
    'sfc': 'snes', 'superfamicom': 'snes', 'famicom': 'nes',
    'genesis': 'megadrive', 'md': 'megadrive', 'sms': 'mastersystem', 'gg': 'gamegear',
    'ps1': 'psx', 'playstation': 'psx', 'ds': 'nds',
}


class Rom(db.Model):
    __tablename__ = 'roms'
    id = db.Column(db.Integer, primary_key=True)
    platform = db.Column(db.String(32), nullable=False, index=True)
    name = db.Column(db.String(512), nullable=False)
    relpath = db.Column(db.String(1024), nullable=False, unique=True)
    size = db.Column(db.BigInteger, nullable=False, default=0)
    added_at = db.Column(db.DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None))


roms_blueprint = Blueprint('roms', __name__)
_scan_lock = threading.Lock()


def platform_for_folder(folder):
    key = folder.lower().replace(' ', '').replace('-', '').replace('_', '')
    key = FOLDER_ALIASES.get(key, key)
    return key if key in PLATFORMS else None


def display_name(filename):
    return os.path.splitext(filename)[0].strip()


def scan_roms(root=None):
    """Sync the roms table with the folder tree. Returns (added, removed, total)."""
    root = root or ROMS_ROOT
    if not os.path.isdir(root):
        logger.info(f'ROM library: {root} not found, skipping scan')
        return 0, 0, 0
    with _scan_lock:
        found = {}
        for entry in os.scandir(root):
            if not entry.is_dir():
                continue
            platform = platform_for_folder(entry.name)
            if not platform:
                continue
            exts = PLATFORMS[platform][2]
            for dirpath, _dirs, files in os.walk(entry.path):
                for fn in files:
                    if fn.startswith('.') or fn.rsplit('.', 1)[-1].lower() not in exts:
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, root)
                    found[rel] = (platform, display_name(fn), os.path.getsize(full))

        existing = {r.relpath: r for r in Rom.query.all()}
        removed = 0
        for rel, row in existing.items():
            if rel not in found:
                db.session.delete(row)
                removed += 1
        added = 0
        for rel, (platform, name, size) in found.items():
            row = existing.get(rel)
            if row:
                row.size = size
            else:
                db.session.add(Rom(platform=platform, name=name, relpath=rel, size=size))
                added += 1
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            logger.warning('ROM library: concurrent scan conflict, will settle on next scan')
        logger.info(f'ROM library scan: {added} added, {removed} removed, {len(found)} total')
        from rom_requests import fulfil_matching_requests
        fulfil_matching_requests()
        return added, removed, len(found)


def scan_roms_in_background(app):
    def _run():
        with app.app_context():
            try:
                scan_roms()
            except Exception:
                logger.exception('ROM library scan failed')
    threading.Thread(target=_run, daemon=True).start()


def _rom_or_404(rom_id):
    rom = db.session.get(Rom, rom_id)
    if not rom:
        abort(404)
    full = os.path.realpath(os.path.join(ROMS_ROOT, rom.relpath))
    if not full.startswith(os.path.realpath(ROMS_ROOT) + os.sep) or not os.path.isfile(full):
        abort(404)
    return rom, full


def _user_key():
    # Before any admin account exists, auth is off and everyone shares slot 0.
    if current_user.is_authenticated:
        return str(current_user.id)
    return '0'


def _state_path(rom_id):
    return os.path.join(STATES_DIR, _user_key(), f'{int(rom_id)}.state')


# ---- Pages ----

@roms_blueprint.route('/roms')
@access_required('shop')
def roms_page():
    roms = Rom.query.order_by(Rom.platform, Rom.name).all()
    grouped = {}
    for rom in roms:
        grouped.setdefault(rom.platform, []).append(rom)
    sections = [(key, PLATFORMS[key][0], grouped[key]) for key in PLATFORMS if key in grouped]
    return render_template('roms.html', title='Retro', sections=sections,
                           roms_root=ROMS_ROOT, admin_account_created=admin_account_created())


@roms_blueprint.route('/roms/play/<int:rom_id>')
@access_required('shop')
def play_page(rom_id):
    rom, _ = _rom_or_404(rom_id)
    return render_template('roms_play.html', title='Retro', rom=rom,
                           core=PLATFORMS[rom.platform][1],
                           has_state=os.path.isfile(_state_path(rom_id)),
                           emulatorjs_data=EMULATORJS_DATA,
                           admin_account_created=admin_account_created())


# ---- API ----

@roms_blueprint.route('/api/roms/<int:rom_id>/file')
@access_required('shop')
def rom_file(rom_id):
    rom, full = _rom_or_404(rom_id)
    return send_file(full, as_attachment=request.args.get('download') == '1',
                     download_name=os.path.basename(full), conditional=True)


@roms_blueprint.route('/api/roms/<int:rom_id>/state', methods=['GET', 'PUT', 'DELETE'])
@access_required('shop')
def rom_state(rom_id):
    _rom_or_404(rom_id)
    path = _state_path(rom_id)
    if request.method == 'GET':
        if not os.path.isfile(path):
            abort(404)
        return send_file(path, mimetype='application/octet-stream', max_age=0)
    if request.method == 'DELETE':
        if os.path.isfile(path):
            os.remove(path)
        return jsonify({'success': True})
    data = request.get_data(cache=False)
    if not data or len(data) > MAX_STATE_BYTES:
        return jsonify({'success': False, 'error': 'Empty or oversized state'}), 400
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)
    return jsonify({'success': True, 'size': len(data)})


@roms_blueprint.route('/api/roms/scan', methods=['POST'])
@access_required('admin')
def rom_scan():
    added, removed, total = scan_roms()
    return jsonify({'success': True, 'added': added, 'removed': removed, 'total': total})
