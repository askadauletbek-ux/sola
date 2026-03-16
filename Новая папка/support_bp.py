from flask import Blueprint, request, jsonify, g
from models import db, User, SupportTicket, SupportMessage
from helpers import token_required
import os
from werkzeug.utils import secure_filename
import uuid
from datetime import datetime

support_bp = Blueprint('support', __name__)

UPLOAD_FOLDER = 'static/support_uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)


@support_bp.route('/active_ticket', methods=['GET'])
@token_required
def get_active_ticket():
    user = g.user
    # Ищем открытый тикет
    ticket = SupportTicket.query.filter_by(user_id=user.id, status='open').first()

    if not ticket:
        # Если нет открытого, создаем новый
        ticket = SupportTicket(user_id=user.id, status='open')
        db.session.add(ticket)
        db.session.commit()

    # Собираем сообщения
    messages = []
    # Сортируем сообщения по дате
    sorted_msgs = sorted(ticket.messages, key=lambda x: x.created_at)

    for msg in sorted_msgs:
        messages.append({
            'id': msg.id,
            'sender': msg.sender_type,
            'text': msg.text,
            'image_url': msg.image_url,
            'created_at': msg.created_at.isoformat()
        })

    return jsonify({
        'ticket_id': ticket.id,
        'status': ticket.status,
        'messages': messages
    }), 200


@support_bp.route('/send_message', methods=['POST'])
@token_required
def send_message():
    user = g.user
    ticket_id = request.form.get('ticket_id')
    text = request.form.get('text')
    image = request.files.get('image')

    ticket = SupportTicket.query.filter_by(id=ticket_id, user_id=user.id).first()
    if not ticket or ticket.status != 'open':
        return jsonify({'error': 'Active ticket not found'}), 404

    image_url = None
    if image:
        filename = secure_filename(f"{uuid.uuid4()}_{image.filename}")
        path = os.path.join(UPLOAD_FOLDER, filename)
        image.save(path)
        # URL относительно корня сервера
        image_url = f"/static/support_uploads/{filename}"

    new_msg = SupportMessage(
        ticket_id=ticket.id,
        sender_type='user',
        text=text,
        image_url=image_url
    )

    ticket.updated_at = datetime.utcnow()

    db.session.add(new_msg)
    db.session.commit()

    return jsonify({'status': 'sent', 'image_url': image_url}), 200


@support_bp.route('/history', methods=['GET'])
@token_required
def get_history():
    user = g.user
    tickets = SupportTicket.query.filter_by(user_id=user.id, status='closed').order_by(
        SupportTicket.created_at.desc()).all()

    result = []
    for t in tickets:
        last_msg = SupportMessage.query.filter_by(ticket_id=t.id).order_by(SupportMessage.created_at.desc()).first()
        preview = last_msg.text if last_msg and last_msg.text else (
            "Image" if last_msg and last_msg.image_url else "No messages")

        result.append({
            'id': t.id,
            'created_at': t.created_at.isoformat(),
            'closed_at': t.updated_at.isoformat(),
            'preview': preview
        })

    return jsonify(result), 200


@support_bp.route('/ticket/<int:ticket_id>', methods=['GET'])
@token_required
def get_ticket_details(ticket_id):
    user = g.user
    ticket = SupportTicket.query.filter_by(id=ticket_id, user_id=user.id).first()

    if not ticket:
        return jsonify({'error': 'Ticket not found'}), 404

    messages = []
    sorted_msgs = sorted(ticket.messages, key=lambda x: x.created_at)
    for msg in sorted_msgs:
        messages.append({
            'id': msg.id,
            'sender': msg.sender_type,
            'text': msg.text,
            'image_url': msg.image_url,
            'created_at': msg.created_at.isoformat()
        })

    return jsonify({
        'ticket_id': ticket.id,
        'status': ticket.status,
        'messages': messages
    }), 200