import os
import json
import time
import random
import threading
from flask import Flask, send_from_directory, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

# Thư mục gốc chứa index.html (thư mục cha của backend/)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# Thư mục chứa data — mặc định cùng thư mục backend/, có thể override bằng env EDU_DATA_DIR
# VD: export EDU_DATA_DIR=/app/edu  →  /app/edu/users.json, /app/edu/data.json, /app/edu/students/
DATA_DIR = os.environ.get('EDU_DATA_DIR', os.path.dirname(__file__))
USERS_FILE = os.path.join(DATA_DIR, 'users.json')
DATA_FILE = os.path.join(DATA_DIR, 'data.json')
STUDENTS_DIR = os.path.join(DATA_DIR, 'students')
VISITS_FILE = os.path.join(DATA_DIR, 'visits.json')

app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB cho ảnh base64

# Lock chống race condition khi nhiều request đọc/ghi file cùng lúc
_data_lock = threading.Lock()


# --- Helpers đọc/ghi file users.json ---
def load_users():
    with open(USERS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_users(data):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _is_hashed(pw):
    return pw.startswith('pbkdf2:') or pw.startswith('scrypt:')


def init_passwords():
    """Lần chạy đầu: hash password plaintext trong users.json."""
    data = load_users()
    changed = False
    admin_pw = data['admin'].get('password', '')
    if admin_pw and not _is_hashed(admin_pw):
        data['admin']['password'] = generate_password_hash(admin_pw)
        changed = True
    for t in data.get('teachers', []):
        if t.get('password', '') and not _is_hashed(t['password']):
            t['password'] = generate_password_hash(t['password'])
            changed = True
    if changed:
        save_users(data)


# --- Helpers đọc/ghi classes (data.json chỉ chứa classes) ---
def load_classes():
    if not os.path.exists(DATA_FILE):
        return [
            {'id': '3f2b7a-c1', 'name': 'Lớp Piano Cơ Bản', 'teacherId': 'admin', 'teacherName': 'Thầy Thanh', 'start': '2026-04-01T08:00', 'end': '2026-05-10T17:00', 'desc': 'Lớp học dành cho trẻ em từ 6-12 tuổi.'}
        ]
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    # Tương thích ngược: nếu là cấu trúc cũ có key 'classes'
    if isinstance(data, dict):
        return data.get('classes', [])
    return data


def save_classes(classes):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(classes, f, ensure_ascii=False, indent=2)


# --- Helpers đọc/ghi students theo từng lớp ---
def _students_file(class_id):
    return os.path.join(STUDENTS_DIR, f'students_{class_id}.json')


def load_students(class_id):
    path = _students_file(class_id)
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_students(class_id, students):
    os.makedirs(STUDENTS_DIR, exist_ok=True)
    with open(_students_file(class_id), 'w', encoding='utf-8') as f:
        json.dump(students, f, ensure_ascii=False, indent=2)


def delete_students_file(class_id):
    path = _students_file(class_id)
    if os.path.exists(path):
        os.remove(path)


def load_all_students():
    """Load toàn bộ students từ tất cả file — dùng cho search."""
    all_students = []
    if not os.path.exists(STUDENTS_DIR):
        return all_students
    for fname in os.listdir(STUDENTS_DIR):
        if fname.startswith('students_') and fname.endswith('.json'):
            fpath = os.path.join(STUDENTS_DIR, fname)
            with open(fpath, 'r', encoding='utf-8') as f:
                all_students.extend(json.load(f))
    return all_students


def migrate_old_data():
    """Chuyển data.json cũ (chứa cả students) sang cấu trúc mới."""
    if not os.path.exists(DATA_FILE):
        return
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict) or 'students' not in data:
        return
    old_students = data.get('students', [])
    if old_students:
        # Nhóm students theo classId
        by_class = {}
        for s in old_students:
            cid = s.get('classId', '')
            by_class.setdefault(cid, []).append(s)
        os.makedirs(STUDENTS_DIR, exist_ok=True)
        for cid, sts in by_class.items():
            save_students(cid, sts)
    # Lưu lại data.json chỉ chứa danh sách classes
    save_classes(data.get('classes', []))


# --- Helpers visits ---
def _load_visits():
    if not os.path.exists(VISITS_FILE):
        return []
    with open(VISITS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def _save_visits(visits):
    with open(VISITS_FILE, 'w', encoding='utf-8') as f:
        json.dump(visits, f)


def _cleanup_visits(visits):
    """Giữ lại chỉ các visit trong 24h gần nhất."""
    cutoff = time.time() - 86400
    return [v for v in visits if v > cutoff]


# --- Routes ---
@app.route('/')
def serve_index():
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/eduregis/api/v1/login', methods=['POST'])
def login():
    body = request.get_json(silent=True) or {}
    username = body.get('username', '').strip()
    password = body.get('password', '')
    if not username or not password:
        return jsonify({'ok': False, 'msg': 'Thiếu thông tin.'}), 400

    with _data_lock:
        data = load_users()

    # Kiểm tra admin
    admin = data['admin']
    if username == admin['username'] and check_password_hash(admin['password'], password):
        return jsonify({'ok': True, 'role': 'admin', 'userId': 'admin', 'name': 'Admin'})

    # Kiểm tra giáo viên
    for t in data.get('teachers', []):
        if t.get('status') == 'deleted':
            continue
        if t['username'] == username and check_password_hash(t['password'], password):
            return jsonify({'ok': True, 'role': 'teacher', 'userId': t['id'], 'name': t['name']})

    return jsonify({'ok': False, 'msg': 'Sai thông tin đăng nhập.'}), 401


# --- CRUD Giáo viên ---
@app.route('/eduregis/api/v1/teachers', methods=['GET'])
def get_teachers():
    with _data_lock:
        data = load_users()
    safe = [{'id': t['id'], 'name': t['name'], 'username': t['username'], 'status': t.get('status', 'created')} for t in data.get('teachers', [])]
    return jsonify(safe)


@app.route('/eduregis/api/v1/teachers', methods=['POST'])
def create_teacher():
    body = request.get_json(silent=True) or {}
    name = body.get('name', '').strip()
    username = body.get('username', '').strip()
    password = body.get('password', '')
    if not name or not username or not password:
        return jsonify({'ok': False, 'msg': 'Thiếu thông tin.'}), 400

    with _data_lock:
        data = load_users()
        if username == data['admin']['username']:
            return jsonify({'ok': False, 'msg': 'Username đã tồn tại.'}), 409
        if any(t['username'] == username for t in data['teachers']):
            return jsonify({'ok': False, 'msg': 'Username đã tồn tại.'}), 409

        tid = 't-' + str(int.from_bytes(os.urandom(4), 'big'))
        new_teacher = {
            'id': tid,
            'name': name,
            'username': username,
            'password': generate_password_hash(password),
            'status': 'created'
        }
        data['teachers'].append(new_teacher)
        save_users(data)
    return jsonify({'ok': True, 'id': tid, 'name': name, 'username': username})


@app.route('/eduregis/api/v1/teachers/<tid>', methods=['PUT'])
def update_teacher(tid):
    body = request.get_json(silent=True) or {}
    with _data_lock:
        data = load_users()
        teacher = next((t for t in data['teachers'] if t['id'] == tid), None)
        if not teacher:
            return jsonify({'ok': False, 'msg': 'Không tìm thấy.'}), 404

        if 'name' in body and body['name'].strip():
            teacher['name'] = body['name'].strip()
        if 'username' in body and body['username'].strip():
            new_un = body['username'].strip()
            if new_un != teacher['username']:
                if new_un == data['admin']['username'] or any(t['username'] == new_un and t['id'] != tid for t in data['teachers']):
                    return jsonify({'ok': False, 'msg': 'Username đã tồn tại.'}), 409
                teacher['username'] = new_un
        if 'password' in body and body['password']:
            teacher['password'] = generate_password_hash(body['password'])
        if 'status' in body and body['status'] in ('created', 'deleted'):
            teacher['status'] = body['status']

        save_users(data)
    return jsonify({'ok': True, 'id': tid, 'name': teacher['name'], 'username': teacher['username']})


@app.route('/eduregis/api/v1/teachers/<tid>', methods=['DELETE'])
def delete_teacher(tid):
    with _data_lock:
        data = load_users()
        teacher = next((t for t in data['teachers'] if t['id'] == tid), None)
        if not teacher:
            return jsonify({'ok': False, 'msg': 'Không tìm thấy.'}), 404
        teacher['status'] = 'deleted'
        save_users(data)
    return jsonify({'ok': True})


# --- CRUD Classes ---
@app.route('/eduregis/api/v1/classes', methods=['GET'])
def get_classes():
    with _data_lock:
        classes = load_classes()
    # Không trả pin cho client — chỉ giữ pinEnabled
    safe = []
    for c in classes:
        sc = dict(c)
        sc.pop('pin', None)
        safe.append(sc)
    return jsonify(safe)


@app.route('/eduregis/api/v1/classes', methods=['POST'])
def create_class():
    body = request.get_json(silent=True) or {}
    start = body.get('start', '')
    end = body.get('end', '')
    if start and end and end <= start:
        return jsonify({'ok': False, 'msg': 'Thời gian đóng phải sau thời gian mở.'}), 400
    with _data_lock:
        classes = load_classes()
        cid = body.get('id') or os.urandom(5).hex()
        cls = {
            'id': cid,
            'name': body.get('name', ''),
            'teacherId': body.get('teacherId', 'admin'),
            'teacherName': body.get('teacherName', 'Admin'),
            'start': body.get('start', ''),
            'end': body.get('end', ''),
            'desc': body.get('desc', ''),
            'pinEnabled': bool(body.get('pinEnabled', False)),
            'pin': body.get('pin', ''),
            'image': body.get('image', '')
        }
        idx = next((i for i, c in enumerate(classes) if c['id'] == cid), -1)
        if idx > -1:
            # Giữ PIN cũ nếu không gửi pin mới
            if cls['pinEnabled'] and not cls['pin']:
                cls['pin'] = classes[idx].get('pin', '')
            # Giữ ảnh cũ nếu không gửi ảnh mới
            if not cls['image']:
                cls['image'] = classes[idx].get('image', '')
            classes[idx] = cls
        else:
            classes.insert(0, cls)
        save_classes(classes)
    return jsonify({'ok': True, 'class': cls})


@app.route('/eduregis/api/v1/classes/<cid>', methods=['DELETE'])
def delete_class(cid):
    with _data_lock:
        classes = load_classes()
        classes = [c for c in classes if c['id'] != cid]
        save_classes(classes)
        delete_students_file(cid)
    return jsonify({'ok': True})


# --- CRUD Students ---
@app.route('/eduregis/api/v1/students', methods=['GET'])
def get_students():
    class_id = request.args.get('classId')
    with _data_lock:
        if class_id:
            return jsonify(load_students(class_id))
        return jsonify(load_all_students())


@app.route('/eduregis/api/v1/students', methods=['POST'])
def create_student():
    body = request.get_json(silent=True) or {}
    class_id = body.get('classId', '')
    if not class_id:
        return jsonify({'ok': False, 'msg': 'Thiếu classId.'}), 400
    with _data_lock:
        classes = load_classes()
        cls = next((c for c in classes if c['id'] == class_id), None)
        if not cls:
            return jsonify({'ok': False, 'msg': 'Lớp không tồn tại.'}), 404
        if cls.get('pinEnabled') and body.get('pin', '') != cls.get('pin', ''):
            return jsonify({'ok': False, 'msg': 'Mã PIN không đúng.'}), 403
        students = load_students(class_id)
        # Tạo mã 6 ký tự unique (check toàn bộ)
        chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
        all_students = load_all_students()
        existing = {s['confirmCode'] for s in all_students}
        code = ''
        while not code or code in existing:
            code = ''.join(random.choices(chars, k=6))
        student = {
            'id': str(int.from_bytes(os.urandom(6), 'big')),
            'classId': class_id,
            'name': body.get('name', ''),
            'dob': body.get('dob', ''),
            'gender': body.get('gender', ''),
            'email': body.get('email', ''),
            'phone': body.get('phone', ''),
            'regTime': body.get('regTime', ''),
            'status': 'pending',
            'confirmCode': code
        }
        students.insert(0, student)
        save_students(class_id, students)
    return jsonify({'ok': True, 'student': student})


@app.route('/eduregis/api/v1/students/<sid>/status', methods=['PUT'])
def update_student_status(sid):
    body = request.get_json(silent=True) or {}
    class_id = body.get('classId', '')
    new_status = body.get('status', '')
    if new_status not in ('pending', 'confirmed', 'attending'):
        return jsonify({'ok': False, 'msg': 'Trạng thái không hợp lệ.'}), 400
    with _data_lock:
        if class_id:
            students = load_students(class_id)
            student = next((s for s in students if s['id'] == sid), None)
            if student:
                student['status'] = new_status
                save_students(class_id, students)
                return jsonify({'ok': True})
        # Fallback: tìm trong tất cả file
        if os.path.exists(STUDENTS_DIR):
            for fname in os.listdir(STUDENTS_DIR):
                if not fname.endswith('.json'):
                    continue
                fpath = os.path.join(STUDENTS_DIR, fname)
                with open(fpath, 'r', encoding='utf-8') as f:
                    sts = json.load(f)
                student = next((s for s in sts if s['id'] == sid), None)
                if student:
                    student['status'] = new_status
                    with open(fpath, 'w', encoding='utf-8') as f:
                        json.dump(sts, f, ensure_ascii=False, indent=2)
                    return jsonify({'ok': True})
    return jsonify({'ok': False, 'msg': 'Không tìm thấy.'}), 404


@app.route('/eduregis/api/v1/students/<sid>', methods=['DELETE'])
def delete_student(sid):
    class_id = request.args.get('classId', '')
    with _data_lock:
        if class_id:
            students = load_students(class_id)
            students = [s for s in students if s['id'] != sid]
            save_students(class_id, students)
            return jsonify({'ok': True})
        if os.path.exists(STUDENTS_DIR):
            for fname in os.listdir(STUDENTS_DIR):
                if not fname.endswith('.json'):
                    continue
                fpath = os.path.join(STUDENTS_DIR, fname)
                with open(fpath, 'r', encoding='utf-8') as f:
                    sts = json.load(f)
                new_sts = [s for s in sts if s['id'] != sid]
                if len(new_sts) < len(sts):
                    with open(fpath, 'w', encoding='utf-8') as f:
                        json.dump(new_sts, f, ensure_ascii=False, indent=2)
                    return jsonify({'ok': True})
    return jsonify({'ok': True})


@app.route('/eduregis/api/v1/change-password', methods=['POST'])
def change_password():
    body = request.get_json(silent=True) or {}
    user_id = body.get('userId', '')
    role = body.get('role', '')
    old_pw = body.get('oldPassword', '')
    new_pw = body.get('newPassword', '')
    if not old_pw or not new_pw:
        return jsonify({'ok': False, 'msg': 'Thiếu thông tin.'}), 400
    if len(new_pw) < 4:
        return jsonify({'ok': False, 'msg': 'Mật khẩu mới tối thiểu 4 ký tự.'}), 400

    with _data_lock:
        data = load_users()

        if role == 'admin':
            if not check_password_hash(data['admin']['password'], old_pw):
                return jsonify({'ok': False, 'msg': 'Mật khẩu hiện tại không đúng.'}), 401
            data['admin']['password'] = generate_password_hash(new_pw)
            save_users(data)
            return jsonify({'ok': True, 'msg': 'Đã đổi mật khẩu Admin.'})

        teacher = next((t for t in data['teachers'] if t['id'] == user_id), None)
        if not teacher:
            return jsonify({'ok': False, 'msg': 'Không tìm thấy tài khoản.'}), 404
        if not check_password_hash(teacher['password'], old_pw):
            return jsonify({'ok': False, 'msg': 'Mật khẩu hiện tại không đúng.'}), 401
        teacher['password'] = generate_password_hash(new_pw)
        save_users(data)
    return jsonify({'ok': True, 'msg': 'Đã đổi mật khẩu.'})


@app.route('/eduregis/api/v1/visits', methods=['POST'])
def record_visit():
    with _data_lock:
        visits = _cleanup_visits(_load_visits())
        visits.append(time.time())
        _save_visits(visits)
    return jsonify({'ok': True})


@app.route('/eduregis/api/v1/visits', methods=['GET'])
def get_visits():
    with _data_lock:
        visits = _cleanup_visits(_load_visits())
        _save_visits(visits)
    return jsonify({'count': len(visits)})


@app.route('/eduregis/api/v1/students/search/<code>', methods=['GET'])
def search_student(code):
    with _data_lock:
        all_students = load_all_students()
        student = next((s for s in all_students if s['confirmCode'] == code.upper()), None)
        if not student:
            return jsonify({'ok': False, 'msg': 'Không tìm thấy.'}), 404
        classes = load_classes()
    cls = next((c for c in classes if c['id'] == student['classId']), None)
    return jsonify({'ok': True, 'student': student, 'class': cls})


if __name__ == '__main__':
    init_passwords()
    migrate_old_data()
    app.run(host='0.0.0.0', port=5000)
