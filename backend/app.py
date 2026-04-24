import os
import io
import time
import uuid
import zipfile
import base64
from datetime import datetime
from pathlib import Path

import numpy as np
import psycopg2
import psycopg2.extras
import pydicom
from PIL import Image, ImageEnhance
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Image as RLImage, Paragraph, Spacer, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

app = Flask(__name__)
CORS(app)

UPLOAD_DIR = "/data/uploads"
PDF_DIR = "/data/pdfs"

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PDF_DIR, exist_ok=True)

# ── PostgreSQL connection ──────────────────────────────────────────────────────
DB_CONFIG = {
    "host":     os.environ.get("POSTGRES_HOST", "db"),
    "port":     int(os.environ.get("POSTGRES_PORT", 5432)),
    "dbname":   os.environ.get("POSTGRES_DB",   "dicomlaudo"),
    "user":     os.environ.get("POSTGRES_USER",  "dicom"),
    "password": os.environ.get("POSTGRES_PASSWORD", "dicom123"),
}


def get_db():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db(retries=10, delay=3):
    """Wait for Postgres to be ready, then create tables."""
    for attempt in range(retries):
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS laudos (
                    id          SERIAL PRIMARY KEY,
                    patient_name  TEXT,
                    patient_dob   TEXT,
                    patient_sex   TEXT,
                    study_date    TEXT,
                    clinic        TEXT,
                    doctor        TEXT,
                    crm           TEXT,
                    exam_type     TEXT,
                    findings      TEXT,
                    conclusion    TEXT,
                    num_images    INTEGER,
                    pdf_path      TEXT,
                    created_at    TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.commit()
            cur.close()
            conn.close()
            print(f"[DB] Connected to PostgreSQL on attempt {attempt + 1}")
            return
        except psycopg2.OperationalError as e:
            print(f"[DB] Postgres not ready (attempt {attempt + 1}/{retries}): {e}")
            time.sleep(delay)
    raise RuntimeError("Could not connect to PostgreSQL after multiple retries")


init_db()


def dcm_to_image(ds, brightness=0, contrast=1.0, clahe=False):
    """Convert DICOM dataset to a PIL Image with proper windowing."""
    arr = ds.pixel_array.astype(np.float32)

    # Apply rescale slope/intercept if present
    slope = float(getattr(ds, 'RescaleSlope', 1) or 1)
    intercept = float(getattr(ds, 'RescaleIntercept', 0) or 0)
    arr = arr * slope + intercept

    # Try window center/width from DICOM
    wc = getattr(ds, 'WindowCenter', None)
    ww = getattr(ds, 'WindowWidth', None)

    if wc is not None and ww is not None:
        if hasattr(wc, '__iter__'):
            wc = float(wc[0])
        else:
            wc = float(wc)
        if hasattr(ww, '__iter__'):
            ww = float(ww[0])
        else:
            ww = float(ww)

        low = wc - ww / 2
        high = wc + ww / 2
        arr = np.clip((arr - low) / (high - low) * 255, 0, 255)
    else:
        # Percentile-based normalization for best visibility
        p2 = np.percentile(arr, 2)
        p98 = np.percentile(arr, 98)
        if p98 > p2:
            arr = np.clip((arr - p2) / (p98 - p2) * 255, 0, 255)
        else:
            arr = np.clip(arr, 0, 255)

    arr = arr.astype(np.uint8)

    photometric = getattr(ds, 'PhotometricInterpretation', 'MONOCHROME2')
    if photometric == 'MONOCHROME1':
        arr = 255 - arr

    # Handle RGB DICOM
    if len(arr.shape) == 3 and arr.shape[2] == 3:
        img = Image.fromarray(arr, mode='RGB')
    else:
        img = Image.fromarray(arr, mode='L').convert('RGB')

    # Apply brightness
    if brightness != 0:
        factor = 1.0 + brightness / 100.0
        img = ImageEnhance.Brightness(img).enhance(max(0.1, factor))

    # Apply contrast
    if contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(max(0.1, contrast))

    return img


def parse_dcm_meta(ds):
    def safe(attr, default=''):
        try:
            val = getattr(ds, attr, default)
            return str(val).strip() if val else default
        except Exception:
            return default

    return {
        'patientName': safe('PatientName'),
        'patientID': safe('PatientID'),
        'patientSex': safe('PatientSex'),
        'patientBirthDate': safe('PatientBirthDate'),
        'studyDate': safe('StudyDate'),
        'studyTime': safe('StudyTime'),
        'modality': safe('Modality', 'US'),
        'clinic': safe('InstitutionName'),
        'referringDoctor': safe('ReferringPhysicianName'),
        'studyDescription': safe('StudyDescription'),
        'seriesDescription': safe('SeriesDescription'),
        'manufacturer': safe('Manufacturer'),
    }


@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


@app.route('/api/upload', methods=['POST'])
def upload():
    files = request.files.getlist('files')
    results = []

    for f in files:
        filename = f.filename or ''
        ext = Path(filename).suffix.lower()

        if ext == '.zip':
            zdata = f.read()
            with zipfile.ZipFile(io.BytesIO(zdata)) as zf:
                for name in sorted(zf.namelist()):
                    if name.lower().endswith('.dcm') and not name.startswith('__MACOSX'):
                        dcm_bytes = zf.read(name)
                        result = process_dcm_bytes(dcm_bytes, Path(name).name)
                        if result:
                            results.append(result)

        elif ext == '.dcm':
            dcm_bytes = f.read()
            result = process_dcm_bytes(dcm_bytes, filename)
            if result:
                results.append(result)

    return jsonify({'images': results})


def process_dcm_bytes(dcm_bytes, name):
    try:
        ds = pydicom.dcmread(io.BytesIO(dcm_bytes))
        img = dcm_to_image(ds)
        meta = parse_dcm_meta(ds)

        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=88)
        b64 = base64.b64encode(buf.getvalue()).decode()

        return {
            'id': str(uuid.uuid4()),
            'name': name,
            'width': img.width,
            'height': img.height,
            'imageData': f'data:image/jpeg;base64,{b64}',
            'meta': meta,
        }
    except Exception as e:
        print(f'Error processing {name}: {e}')
        return None


@app.route('/api/adjust', methods=['POST'])
def adjust():
    """Re-process a DCM file with new brightness/contrast settings."""
    data = request.json
    dcm_b64 = data.get('dcmData')  # original DCM as base64
    brightness = float(data.get('brightness', 0))
    contrast = float(data.get('contrast', 1.0))

    dcm_bytes = base64.b64decode(dcm_b64)
    ds = pydicom.dcmread(io.BytesIO(dcm_bytes))
    img = dcm_to_image(ds, brightness=brightness, contrast=contrast)

    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=88)
    b64 = base64.b64encode(buf.getvalue()).decode()

    return jsonify({'imageData': f'data:image/jpeg;base64,{b64}'})


@app.route('/api/generate-pdf', methods=['POST'])
def generate_pdf():
    data = request.json
    patient_name = data.get('patientName', '')
    patient_dob = data.get('patientDob', '')
    patient_sex = data.get('patientSex', '')
    study_date = data.get('studyDate', '')
    clinic = data.get('clinic', '')
    doctor = data.get('doctor', '')
    crm = data.get('crm', '')
    exam_type = data.get('examType', '')
    findings = data.get('findings', '')
    conclusion = data.get('conclusion', '')
    images_b64 = data.get('images', [])  # list of base64 JPEG strings

    pdf_id = str(uuid.uuid4())
    pdf_filename = f"laudo_{pdf_id}.pdf"
    pdf_path = os.path.join(PDF_DIR, pdf_filename)

    _build_pdf(
        pdf_path,
        patient_name, patient_dob, patient_sex,
        study_date, clinic, doctor, crm,
        exam_type, findings, conclusion, images_b64
    )

    # Save to DB
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO laudos (patient_name, patient_dob, patient_sex, study_date,
            clinic, doctor, crm, exam_type, findings, conclusion,
            num_images, pdf_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        patient_name, patient_dob, patient_sex, study_date,
        clinic, doctor, crm, exam_type, findings, conclusion,
        len(images_b64), pdf_filename,
    ))
    conn.commit()
    cur.close()
    conn.close()

    return send_file(pdf_path, mimetype='application/pdf',
                     as_attachment=True,
                     download_name=f"laudo_{patient_name.replace(' ', '_') or 'paciente'}.pdf")


def _build_pdf(pdf_path, patient_name, patient_dob, patient_sex,
               study_date, clinic, doctor, crm, exam_type,
               findings, conclusion, images_b64):

    from reportlab.platypus import BaseDocTemplate, PageTemplate, Frame
    from reportlab.lib.colors import HexColor

    W, H = A4  # 595.27 x 841.89 pt
    MARGIN = 14 * mm
    BLUE_DARK = HexColor('#1a3a5c')
    BLUE_LIGHT = HexColor('#e8eef5')
    GRAY = HexColor('#f5f7fa')

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=60 * mm, bottomMargin=20 * mm,
    )

    styles = getSampleStyleSheet()
    normal = ParagraphStyle('n', fontName='Helvetica', fontSize=9, leading=13)
    bold = ParagraphStyle('b', fontName='Helvetica-Bold', fontSize=9, leading=13)
    section_title = ParagraphStyle('st', fontName='Helvetica-Bold', fontSize=8,
                                   textColor=BLUE_DARK, leading=12, spaceAfter=2)

    def draw_header_footer(canvas, doc):
        canvas.saveState()
        # Header background
        canvas.setFillColor(BLUE_DARK)
        canvas.rect(0, H - 28 * mm, W, 28 * mm, fill=1, stroke=0)

        # Clinic name
        canvas.setFillColor(colors.white)
        canvas.setFont('Helvetica-Bold', 14)
        canvas.drawString(MARGIN, H - 12 * mm, clinic or 'Clínica')
        canvas.setFont('Helvetica', 8)
        canvas.drawString(MARGIN, H - 18 * mm, f'Laudo de Imagem — {exam_type or "Ultrassonografia"}')

        # Date top right
        date_str = format_date(study_date) or datetime.now().strftime('%d/%m/%Y')
        canvas.setFont('Helvetica', 8)
        canvas.drawRightString(W - MARGIN, H - 12 * mm, date_str)

        # Patient bar
        canvas.setFillColor(GRAY)
        canvas.rect(0, H - 46 * mm, W, 18 * mm, fill=1, stroke=0)
        canvas.setStrokeColor(HexColor('#dde3ea'))
        canvas.setLineWidth(0.5)
        canvas.line(0, H - 46 * mm, W, H - 46 * mm)

        canvas.setFillColor(HexColor('#888888'))
        canvas.setFont('Helvetica', 7)
        canvas.drawString(MARGIN, H - 33 * mm, 'PACIENTE')
        canvas.drawString(MARGIN + 80 * mm, H - 33 * mm, 'NASCIMENTO')
        canvas.drawString(MARGIN + 145 * mm, H - 33 * mm, 'SEXO')

        canvas.setFillColor(HexColor('#1a1a1a'))
        canvas.setFont('Helvetica-Bold', 10)
        canvas.drawString(MARGIN, H - 38 * mm, patient_name or '—')
        canvas.setFont('Helvetica', 10)
        canvas.drawString(MARGIN + 80 * mm, H - 38 * mm, format_date(patient_dob) or '—')
        canvas.drawString(MARGIN + 145 * mm, H - 38 * mm, patient_sex or '—')

        # Footer
        canvas.setFillColor(HexColor('#888888'))
        canvas.setFont('Helvetica', 7)
        canvas.drawString(MARGIN, 12 * mm, f'Página {doc.page}')
        canvas.setStrokeColor(HexColor('#cccccc'))
        canvas.setLineWidth(0.3)
        canvas.line(MARGIN, 18 * mm, W - MARGIN, 18 * mm)

        # Signature line
        sig_x = W - MARGIN - 60 * mm
        canvas.setStrokeColor(HexColor('#555555'))
        canvas.setLineWidth(0.5)
        canvas.line(sig_x, 22 * mm, W - MARGIN, 22 * mm)
        canvas.setFillColor(HexColor('#1a1a1a'))
        canvas.setFont('Helvetica-Bold', 8)
        canvas.drawCentredString(sig_x + 30 * mm, 18 * mm, doctor or 'Médico Responsável')
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(HexColor('#888888'))
        canvas.drawCentredString(sig_x + 30 * mm, 14 * mm, crm or '')

        canvas.restoreState()

    story = []
    usable_w = W - 2 * MARGIN

    # Images: 2 per page
    imgs_per_page = 2
    img_h_pt = (H - 46 * mm - 75 * mm) / imgs_per_page  # available height per image slot

    # Group images into pairs
    from reportlab.platypus import KeepTogether, PageBreak
    for i in range(0, len(images_b64), imgs_per_page):
        chunk = images_b64[i:i + imgs_per_page]
        for j, img_b64 in enumerate(chunk):
            raw = base64.b64decode(img_b64.split(',')[1] if ',' in img_b64 else img_b64)
            pil = Image.open(io.BytesIO(raw))
            aspect = pil.width / pil.height
            draw_w = usable_w
            draw_h = min(img_h_pt - 10, draw_w / aspect)

            img_buf = io.BytesIO(raw)
            rl_img = RLImage(img_buf, width=draw_w, height=draw_h)
            story.append(rl_img)

            img_num = i + j + 1
            story.append(Paragraph(f'<font color="#888888">Imagem {img_num} — {images_b64[i+j] and "DCM"}</font>', normal))
            story.append(Spacer(1, 4))

        if i + imgs_per_page < len(images_b64):
            story.append(PageBreak())

    # Findings & conclusion on last page
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width='100%', thickness=0.5, color=HexColor('#dde3ea')))
    story.append(Spacer(1, 4))
    story.append(Paragraph('ACHADOS', section_title))
    story.append(Paragraph(findings or '—', normal))
    story.append(Spacer(1, 8))
    story.append(Paragraph('CONCLUSÃO', section_title))
    story.append(Paragraph(conclusion or '—', normal))

    doc.build(story, onFirstPage=draw_header_footer, onLaterPages=draw_header_footer)


def format_date(d):
    if not d:
        return ''
    d = d.replace('-', '')
    if len(d) == 8:
        return f'{d[6:8]}/{d[4:6]}/{d[0:4]}'
    return d


@app.route('/api/laudos', methods=['GET'])
def list_laudos():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, patient_name, patient_dob, study_date, clinic,
               exam_type, num_images, created_at
        FROM laudos
        ORDER BY id DESC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    # Convert datetime objects to ISO strings for JSON serialisation
    result = []
    for r in rows:
        row = dict(r)
        if row.get('created_at'):
            row['created_at'] = row['created_at'].isoformat()
        result.append(row)
    return jsonify({'laudos': result})


@app.route('/api/laudos/<int:laudo_id>/pdf', methods=['GET'])
def download_laudo(laudo_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM laudos WHERE id = %s", (laudo_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    pdf_path = os.path.join(PDF_DIR, row['pdf_path'])
    if not os.path.exists(pdf_path):
        return jsonify({'error': 'PDF not found on disk'}), 404
    safe_name = (row['patient_name'] or 'paciente').replace(' ', '_')
    return send_file(pdf_path, mimetype='application/pdf',
                     as_attachment=True,
                     download_name=f"laudo_{safe_name}.pdf")


@app.route('/api/laudos/<int:laudo_id>', methods=['DELETE'])
def delete_laudo(laudo_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT pdf_path FROM laudos WHERE id = %s", (laudo_id,))
    row = cur.fetchone()
    if row:
        try:
            os.remove(os.path.join(PDF_DIR, row['pdf_path']))
        except Exception:
            pass
        cur.execute("DELETE FROM laudos WHERE id = %s", (laudo_id,))
        conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
