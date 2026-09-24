"""Game requests for the retro library, Seerr-style.

Users ask for games; admins approve, decline or fulfil them. Fulfilment is manual
(buy, dump, drop into /roms) - a rescan that finds a matching ROM closes the request
automatically and notifies the requester via an optional Discord-compatible webhook.
"""
import datetime
import logging
import os
import re
import threading

import requests as http
from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user

from auth import access_required, admin_account_created
from db import db

logger = logging.getLogger('main')

WEBHOOK_URL = os.environ.get('OWNFOIL_REQUESTS_WEBHOOK', '').strip()
STATUSES = ('pending', 'approved', 'declined', 'fulfilled')
OPEN_STATUSES = ('pending', 'approved')
MAX_TITLE = 200
MAX_NOTE = 500


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class RomRequest(db.Model):
    __tablename__ = 'rom_requests'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True, index=True)
    username = db.Column(db.String(100), nullable=False)
    title = db.Column(db.String(MAX_TITLE), nullable=False)
    platform = db.Column(db.String(32), nullable=False)
    note = db.Column(db.String(MAX_NOTE), nullable=True)
    status = db.Column(db.String(16), nullable=False, default='pending', index=True)
    admin_note = db.Column(db.String(MAX_NOTE), nullable=True)
    rom_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    def to_dict(self):
        from roms import PLATFORMS
        return {
            'id': self.id, 'username': self.username, 'title': self.title,
            'platform': self.platform, 'platform_name': PLATFORMS.get(self.platform, (self.platform,))[0],
            'note': self.note, 'status': self.status, 'admin_note': self.admin_note,
            'rom_id': self.rom_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


requests_blueprint = Blueprint('rom_requests', __name__)


# ---- helpers ----

def normalize_title(title):
    """'Golden Sun (USA, Europe) [!]' -> 'golden sun'"""
    t = re.sub(r'[\(\[][^\)\]]*[\)\]]', ' ', title.lower())
    t = t.replace('&', ' and ')
    t = re.sub(r'[^a-z0-9]+', ' ', t)
    return ' '.join(t.split())


def _is_admin():
    return not admin_account_created() or (current_user.is_authenticated and current_user.is_admin)


def _current_identity():
    if current_user.is_authenticated:
        return current_user.id, current_user.user
    return None, 'guest'


def _owns(req):
    user_id, _ = _current_identity()
    return req.user_id == user_id


def notify(message):
    """Fire-and-forget Discord-compatible webhook."""
    if not WEBHOOK_URL:
        return

    def _send():
        try:
            http.post(WEBHOOK_URL, json={'content': message}, timeout=10)
        except Exception as e:
            logger.warning(f'Request webhook failed: {e}')
    threading.Thread(target=_send, daemon=True).start()


def fulfil_matching_requests():
    """Close open requests whose title+platform now match a ROM in the library."""
    from roms import Rom
    open_reqs = RomRequest.query.filter(RomRequest.status.in_(OPEN_STATUSES)).all()
    if not open_reqs:
        return 0
    by_key = {}
    for rom in Rom.query.all():
        by_key.setdefault((rom.platform, normalize_title(rom.name)), rom)
    closed = 0
    for req in open_reqs:
        rom = by_key.get((req.platform, normalize_title(req.title)))
        if rom:
            req.status = 'fulfilled'
            req.rom_id = rom.id
            closed += 1
            notify(f'✅ Request fulfilled: **{req.title}** ({req.platform}) for {req.username} is now in the library.')
    if closed:
        db.session.commit()
        logger.info(f'ROM requests: {closed} fulfilled by library scan')
    return closed


# ---- page ----

@requests_blueprint.route('/roms/requests')
@access_required('shop')
def requests_page():
    from roms import PLATFORMS
    return render_template('rom_requests.html', title='Retro',
                           platforms=[(k, v[0]) for k, v in PLATFORMS.items()],
                           is_admin=_is_admin(),
                           admin_account_created=admin_account_created())


# ---- API ----

@requests_blueprint.route('/api/roms/requests', methods=['GET'])
@access_required('shop')
def list_requests():
    q = RomRequest.query
    if not _is_admin():
        user_id, _ = _current_identity()
        q = q.filter(RomRequest.user_id == user_id)
    status = request.args.get('status')
    if status in STATUSES:
        q = q.filter(RomRequest.status == status)
    rows = q.order_by(RomRequest.created_at.desc()).all()
    return jsonify({'requests': [r.to_dict() for r in rows]})


@requests_blueprint.route('/api/roms/requests', methods=['POST'])
@access_required('shop')
def create_request():
    from roms import PLATFORMS
    data = request.get_json(silent=True) or {}
    title = (data.get('title') or '').strip()
    platform = (data.get('platform') or '').strip()
    note = (data.get('note') or '').strip()[:MAX_NOTE] or None
    if not title or len(title) > MAX_TITLE:
        return jsonify({'success': False, 'error': 'Title is required (max 200 characters).'}), 400
    if platform not in PLATFORMS:
        return jsonify({'success': False, 'error': 'Unknown platform.'}), 400

    # Already in the library?
    from roms import Rom
    wanted = normalize_title(title)
    for rom in Rom.query.filter_by(platform=platform).all():
        if normalize_title(rom.name) == wanted:
            return jsonify({'success': False, 'error': f'"{rom.name}" is already in the library.',
                            'rom_id': rom.id}), 409

    # Already requested and still open? Return that one instead of duplicating.
    for existing in RomRequest.query.filter(RomRequest.platform == platform,
                                            RomRequest.status.in_(OPEN_STATUSES)).all():
        if normalize_title(existing.title) == wanted:
            return jsonify({'success': True, 'duplicate': True, 'request': existing.to_dict()})

    user_id, username = _current_identity()
    req = RomRequest(user_id=user_id, username=username, title=title, platform=platform, note=note)
    db.session.add(req)
    db.session.commit()
    notify(f'🎮 New request from {username}: **{title}** ({PLATFORMS[platform][0]})')
    return jsonify({'success': True, 'request': req.to_dict()}), 201


@requests_blueprint.route('/api/roms/requests/<int:req_id>', methods=['PATCH'])
@access_required('shop')
def update_request(req_id):
    if not _is_admin():
        return jsonify({'success': False, 'error': 'Admin only.'}), 403
    req = db.session.get(RomRequest, req_id)
    if not req:
        return jsonify({'success': False, 'error': 'Not found.'}), 404
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status is not None:
        if status not in STATUSES:
            return jsonify({'success': False, 'error': 'Invalid status.'}), 400
        if status != req.status:
            req.status = status
            if status in ('approved', 'declined', 'fulfilled'):
                icon = {'approved': '👍', 'declined': '❌', 'fulfilled': '✅'}[status]
                notify(f'{icon} Request **{req.title}** for {req.username} was {status}.')
    if 'admin_note' in data:
        req.admin_note = (data.get('admin_note') or '').strip()[:MAX_NOTE] or None
    db.session.commit()
    return jsonify({'success': True, 'request': req.to_dict()})


@requests_blueprint.route('/api/roms/requests/<int:req_id>', methods=['DELETE'])
@access_required('shop')
def delete_request(req_id):
    req = db.session.get(RomRequest, req_id)
    if not req:
        return jsonify({'success': False, 'error': 'Not found.'}), 404
    if not _is_admin() and not (_owns(req) and req.status == 'pending'):
        return jsonify({'success': False, 'error': 'You can only withdraw your own pending requests.'}), 403
    db.session.delete(req)
    db.session.commit()
    return jsonify({'success': True})
