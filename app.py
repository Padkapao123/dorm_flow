import html
import os
from datetime import date

import pandas as pd
import psycopg2
import psycopg2.extras
import streamlit as st
import streamlit.components.v1 as components

# ---------------------------------------------------------------------------
# ตั้งค่าหน้าเว็บ
# ---------------------------------------------------------------------------
st.set_page_config(page_title="ระบบจัดการหอพัก", page_icon="🏠", layout="centered")

THAI_MONTHS = [
    "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม",
]
ROOMS = list(range(1, 11))  # ห้อง 1 - 10 (แก้จำนวนห้องตรงนี้)
DEFAULT_RATES = {"elec_per_unit": 8.0, "water_per_unit": 18.0, "rent": 3200.0}

TABLE_REAL = "dorm_bills"        # ตารางจริง
TABLE_MOCK = "dorm_bill_mock"    # ตารางทดสอบ (คัดลอกโครงสร้างและข้อมูลจากตารางจริง)
# ตั้ง Environment Variable USE_MOCK=0 ถ้าต้องการให้เริ่มต้นที่ตารางจริง
USE_MOCK_DEFAULT = os.environ.get("USE_MOCK", "1") != "0"

# คอลัมน์ของตารางบิล (ลำดับเดียวกับ schema จริง)
BILL_COLS = [
    "bill_id", "room_id", "bill_date", "elec_meter", "water_meter", "note",
    "created_at", "elec_meter_end", "water_meter_end", "parking_fee", "rent",
    "elec_unit_used", "water_unit_used", "elec_charge", "water_charge", "total_charge",
]


# ---------------------------------------------------------------------------
# ฟังก์ชันช่วยเหลือทั่วไป
# ---------------------------------------------------------------------------
def thai_period(d):
    """แปลงวันที่เป็น 'ตุลาคม 2569' (ปี พ.ศ.)"""
    return f"{THAI_MONTHS[d.month - 1]} {d.year + 543}"


def to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def float_or_none(value):
    return None if value is None else to_float(value)


def money(value):
    return f"{to_float(value):,.2f}"


def current_meters_of(row):
    """เลขมิเตอร์ปัจจุบันของบิลที่มีอยู่แล้ว: ใช้ *_meter_end ถ้ามีค่า ไม่งั้นใช้ *_meter (ข้อมูลเก่า)"""
    elec = to_float(row.get("elec_meter_end"))
    water = to_float(row.get("water_meter_end"))
    if elec <= 0:
        elec = to_float(row.get("elec_meter"))
    if water <= 0:
        water = to_float(row.get("water_meter"))
    return elec, water


# ---------------------------------------------------------------------------
# 1) การเชื่อมต่อฐานข้อมูล (Neon PostgreSQL ผ่าน psycopg2)
# ---------------------------------------------------------------------------
def get_database_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        try:
            url = st.secrets["DATABASE_URL"]
        except Exception:
            url = None
    return url


def get_conn():
    url = get_database_url()
    if not url:
        raise RuntimeError(
            "ไม่พบ DATABASE_URL กรุณาตั้งค่าใน Environment Variable หรือ st.secrets"
        )
    return psycopg2.connect(url, connect_timeout=15)


def db_fetch(sql, params=None):
    """SELECT แล้วคืนค่าเป็น list ของ dict"""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def db_execute(sql, params=None):
    """INSERT / UPDATE / DDL แล้ว commit"""
    conn = get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# สร้างตาราง / ตารางทดสอบ
# ---------------------------------------------------------------------------
def copy_real_to_mock():
    """คัดลอกข้อมูลจากตารางจริงไปตารางทดสอบ (ไม่แก้ตารางจริง)"""
    cols = ", ".join(BILL_COLS)
    db_execute(
        f"""
        INSERT INTO {TABLE_MOCK} ({cols})
        SELECT {cols} FROM {TABLE_REAL}
        ON CONFLICT (bill_id) DO NOTHING
        """
    )


def reset_mock():
    db_execute(f"DELETE FROM {TABLE_MOCK}")
    copy_real_to_mock()


def ensure_tables():
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS dorm_rates (
            valid_from     DATE PRIMARY KEY,
            elec_per_unit  NUMERIC(10,2) NOT NULL,
            water_per_unit NUMERIC(10,2) NOT NULL,
            rent           NUMERIC(10,2) NOT NULL
        )
        """
    )
    # ตารางจริง (ถ้ามีอยู่แล้วจะไม่ทำอะไร)
    db_execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_REAL} (
            bill_id          VARCHAR(20) PRIMARY KEY,
            room_id          INTEGER NOT NULL,
            bill_date        DATE NOT NULL,
            elec_meter       INTEGER NOT NULL,
            water_meter      NUMERIC(10,2) NOT NULL,
            note             TEXT,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            elec_meter_end   NUMERIC DEFAULT 0,
            water_meter_end  NUMERIC DEFAULT 0,
            parking_fee      NUMERIC DEFAULT 0,
            rent             NUMERIC DEFAULT 3200,
            elec_unit_used   NUMERIC DEFAULT 0,
            water_unit_used  NUMERIC DEFAULT 0,
            elec_charge      NUMERIC DEFAULT 0,
            water_charge     NUMERIC DEFAULT 0,
            total_charge     NUMERIC DEFAULT 0
        )
        """
    )
    # ถ้า dorm_bill_mock เป็นโครงสร้างรุ่นเก่าที่แอปเคยสร้างไว้ (ไม่มีคอลัมน์ elec_meter) ให้ลบทิ้งแล้วสร้างใหม่
    stale = db_fetch(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = 'elec_meter'
        """,
        (TABLE_MOCK,),
    )
    exists = db_fetch(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s", (TABLE_MOCK,)
    )
    if exists and not stale:
        db_execute(f"DROP TABLE {TABLE_MOCK}")
    db_execute(f"CREATE TABLE IF NOT EXISTS {TABLE_MOCK} (LIKE {TABLE_REAL} INCLUDING ALL)")
    count = db_fetch(f"SELECT COUNT(*) AS n FROM {TABLE_MOCK}")[0]["n"]
    if count == 0:
        copy_real_to_mock()


# ---------------------------------------------------------------------------
# 2) ดึงเรทราคาปัจจุบันจากตาราง dorm_rates
# ---------------------------------------------------------------------------
def get_current_rates():
    """คืน (rates, found) โดย rates ดึงจากแถวที่ valid_from ล่าสุด"""
    rows = db_fetch(
        """
        SELECT elec_per_unit, water_per_unit, rent
        FROM dorm_rates
        ORDER BY valid_from DESC
        LIMIT 1
        """
    )
    if not rows:
        return dict(DEFAULT_RATES), False
    r = rows[0]
    return {
        "elec_per_unit": to_float(r["elec_per_unit"]),
        "water_per_unit": to_float(r["water_per_unit"]),
        "rent": to_float(r["rent"]),
    }, True


# ---------------------------------------------------------------------------
# 3) ฟังก์ชันอ่าน/เขียนบิล (รับชื่อตารางเข้ามา เพื่อสลับระหว่างตารางจริง/ทดสอบ)
# ---------------------------------------------------------------------------
def get_prev_row(table, room_id, period):
    """
    บิลล่าสุดก่อนงวดนี้ของห้องนั้น
    เลขมิเตอร์เดือนก่อน = elec_meter_end / water_meter_end ถ้ามีค่า (> 0)
    ถ้าเป็นข้อมูลเก่าที่ยังไม่ได้เติม *_end จะใช้ค่า elec_meter / water_meter แทน
    """
    rows = db_fetch(
        f"""
        SELECT bill_id, bill_date,
               COALESCE(NULLIF(elec_meter_end, 0), elec_meter)   AS prev_elec,
               COALESCE(NULLIF(water_meter_end, 0), water_meter) AS prev_water
        FROM {table}
        WHERE room_id = %s AND bill_date < %s
        ORDER BY bill_date DESC
        LIMIT 1
        """,
        (room_id, period),
    )
    return rows[0] if rows else None


def get_recent_rows(table, room_id, limit=5):
    return db_fetch(
        f"""
        SELECT bill_id, bill_date, elec_meter, water_meter, elec_meter_end, water_meter_end
        FROM {table}
        WHERE room_id = %s
        ORDER BY bill_date DESC
        LIMIT {int(limit)}
        """,
        (room_id,),
    )


def get_bill(table, bill_id):
    rows = db_fetch(f"SELECT * FROM {table} WHERE bill_id = %s", (bill_id,))
    return rows[0] if rows else None


def get_bills_of_room(table, room_id):
    return db_fetch(
        f"SELECT * FROM {table} WHERE room_id = %s ORDER BY bill_date DESC",
        (room_id,),
    )


def get_all_bills(table):
    return db_fetch(f"SELECT * FROM {table} ORDER BY bill_date DESC")


def save_bill(table, bill):
    """บันทึกบิล ถ้า bill_id ซ้ำจะ UPDATE ทับข้อมูลเดิม (created_at คงเดิม)"""
    db_execute(
        f"""
        INSERT INTO {table} (
            bill_id, room_id, bill_date,
            elec_meter, water_meter, note,
            elec_meter_end, water_meter_end,
            parking_fee, rent,
            elec_unit_used, water_unit_used,
            elec_charge, water_charge, total_charge
        ) VALUES (
            %(bill_id)s, %(room_id)s, %(bill_date)s,
            %(elec_meter)s, %(water_meter)s, %(note)s,
            %(elec_meter_end)s, %(water_meter_end)s,
            %(parking_fee)s, %(rent)s,
            %(elec_unit_used)s, %(water_unit_used)s,
            %(elec_charge)s, %(water_charge)s, %(total_charge)s
        )
        ON CONFLICT (bill_id) DO UPDATE SET
            room_id         = EXCLUDED.room_id,
            bill_date       = EXCLUDED.bill_date,
            elec_meter      = EXCLUDED.elec_meter,
            water_meter     = EXCLUDED.water_meter,
            note            = EXCLUDED.note,
            elec_meter_end  = EXCLUDED.elec_meter_end,
            water_meter_end = EXCLUDED.water_meter_end,
            parking_fee     = EXCLUDED.parking_fee,
            rent            = EXCLUDED.rent,
            elec_unit_used  = EXCLUDED.elec_unit_used,
            water_unit_used = EXCLUDED.water_unit_used,
            elec_charge     = EXCLUDED.elec_charge,
            water_charge    = EXCLUDED.water_charge,
            total_charge    = EXCLUDED.total_charge
        """,
        bill,
    )


# ---------------------------------------------------------------------------
# 5) ใบแจ้งหนี้ HTML/CSS (สำหรับแคปเจอร์ส่งไลน์)
# ---------------------------------------------------------------------------
RECEIPT_CSS = """
<link href="https://fonts.googleapis.com/css2?family=Sarabun:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 8px; background: transparent;
         font-family: 'Sarabun', 'Noto Sans Thai', 'Tahoma', sans-serif; }
  .card { max-width: 440px; margin: 0 auto; background: #ffffff;
          border-radius: 18px; overflow: hidden;
          border: 2px solid #0f4c5c; color: #1b2a30; }
  .head { background: #0f4c5c; color: #ffffff; padding: 22px 24px 18px; text-align: center; }
  .head .title { font-size: 26px; font-weight: 700; letter-spacing: 0.5px; }
  .head .period { font-size: 24px; font-weight: 600; margin-top: 6px; color: #ffd166; }
  .meta { display: flex; justify-content: space-between; gap: 10px;
          padding: 16px 24px; background: #eaf3f5; font-size: 20px; }
  .meta b { font-weight: 700; }
  .meta .code { font-size: 17px; color: #4a5f66; align-self: center; }
  .rows { padding: 8px 24px 4px; }
  .row { display: flex; justify-content: space-between; align-items: baseline;
         gap: 12px; padding: 14px 0; border-bottom: 2px dashed #cfdde1;
         font-size: 22px; }
  .row .label { flex: 1; }
  .row .amt { font-weight: 700; white-space: nowrap; }
  .row .unit { font-size: 18px; color: #5a6f76; }
  .total { margin: 14px 18px 22px; padding: 18px 12px; text-align: center;
           background: #fff1f0; border: 2px solid #d62828; border-radius: 14px; }
  .total .t-label { font-size: 22px; font-weight: 600; color: #7a1c1c; }
  .total .t-amt { font-size: 52px; font-weight: 700; color: #d62828; line-height: 1.15; }
  .total .t-unit { font-size: 22px; font-weight: 600; color: #7a1c1c; }
</style>
"""


def build_receipt_html(bill):
    period_text = thai_period(bill["bill_date"])
    room = html.escape(str(bill["room_id"]))
    bill_code = html.escape(str(bill["bill_id"]))

    elec_units = to_float(bill["elec_unit_used"])
    water_units = to_float(bill["water_unit_used"])
    parking = to_float(bill["parking_fee"])

    parking_row = ""
    if parking > 0:
        parking_row = (
            '<div class="row"><span class="label">ค่าที่จอดรถ</span>'
            f'<span class="amt">{money(parking)}</span></div>'
        )

    body = f"""
    <div class="card">
      <div class="head">
        <div class="title">ใบแจ้งค่าเช่าหอพัก</div>
        <div class="period">ประจำเดือน {period_text}</div>
      </div>
      <div class="meta">
        <div><b>ห้อง {room}</b></div>
        <div class="code">รหัสบิล {bill_code}</div>
      </div>
      <div class="rows">
        <div class="row">
          <span class="label">ค่าเช่าห้องพัก</span>
          <span class="amt">{money(bill["rent"])}</span>
        </div>
        <div class="row">
          <span class="label">ค่าน้ำ <span class="unit">({water_units:,.1f} หน่วย)</span></span>
          <span class="amt">{money(bill["water_charge"])}</span>
        </div>
        <div class="row">
          <span class="label">ค่าไฟ <span class="unit">({elec_units:,.1f} หน่วย)</span></span>
          <span class="amt">{money(bill["elec_charge"])}</span>
        </div>
        {parking_row}
      </div>
      <div class="total">
        <div class="t-label">ยอดรวมสุทธิที่ต้องชำระ</div>
        <div class="t-amt">{money(bill["total_charge"])}</div>
        <div class="t-unit">บาท</div>
      </div>
    </div>
    """
    return RECEIPT_CSS + body


# ---------------------------------------------------------------------------
# ส่วนหลักของแอป
# ---------------------------------------------------------------------------
st.title("🏠 ระบบจัดการหอพัก")

if not get_database_url():
    st.error("ไม่พบ DATABASE_URL กรุณาตั้งค่าใน Environment Variable หรือ st.secrets แล้วรีสตาร์ทแอป")
    st.stop()

# ---- แถบด้านข้าง: สลับโหมดทดสอบ / ตารางจริง ----
st.sidebar.header("⚙️ ตั้งค่า")
use_mock = st.sidebar.checkbox(
    f"🧪 โหมดทดสอบ (ใช้ตาราง {TABLE_MOCK})", value=USE_MOCK_DEFAULT, key="use_mock"
)
TABLE = TABLE_MOCK if use_mock else TABLE_REAL

try:
    if not st.session_state.get("tables_ready"):
        ensure_tables()
        st.session_state["tables_ready"] = True
    if use_mock and st.sidebar.button(f"♻️ รีเซ็ต {TABLE_MOCK} ให้เหมือนตารางจริง"):
        reset_mock()
        st.sidebar.success("รีเซ็ตข้อมูลทดสอบแล้ว")
    rates, rates_found = get_current_rates()
except Exception as exc:
    st.error(f"เชื่อมต่อฐานข้อมูลไม่สำเร็จ: {exc}")
    st.stop()

if use_mock:
    st.warning(
        f"🧪 โหมดทดสอบ: ระบบอ่านและบันทึกที่ตาราง {TABLE_MOCK} เท่านั้น "
        f"ไม่กระทบข้อมูลจริง (ติ๊กออกที่เมนูด้านซ้ายเมื่อต้องการใช้งานจริง)"
    )

if not rates_found:
    st.warning(
        "ยังไม่มีข้อมูลในตาราง dorm_rates ระบบจึงใช้ค่าเริ่มต้นชั่วคราว "
        f"(ไฟ {DEFAULT_RATES['elec_per_unit']:.2f} / น้ำ {DEFAULT_RATES['water_per_unit']:.2f} / "
        f"ค่าเช่า {DEFAULT_RATES['rent']:,.0f} บาท)"
    )

tab_bill, tab_history = st.tabs(["📝 บันทึกบิล", "📚 ประวัติบิลทั้งหมด"])

# ===========================================================================
# Tab 1: ฟอร์มบันทึกบิล + ใบแจ้งหนี้
# ===========================================================================
with tab_bill:
    st.subheader("บันทึกและคำนวณบิล")
    st.caption(
        f"เรทปัจจุบัน: ค่าไฟ {rates['elec_per_unit']:,.2f} บาท/หน่วย  |  "
        f"ค่าน้ำ {rates['water_per_unit']:,.2f} บาท/หน่วย  |  "
        f"ค่าเช่า {rates['rent']:,.2f} บาท"
    )

    c1, c2 = st.columns(2)
    with c1:
        room_id = st.selectbox("เลือกห้องพัก", ROOMS, format_func=lambda r: f"ห้อง {r}", key="form_room")
    with c2:
        picked_date = st.date_input("วันที่ / งวดประจำเดือน", value=date.today(), key="form_date")

    period = picked_date.replace(day=1)
    bill_id = f"{period:%Y%m}-{room_id}"  # รูปแบบเดียวกับข้อมูลเดิม เช่น 202510-1

    try:
        prev_row = get_prev_row(TABLE, room_id, period)
        existing = get_bill(TABLE, bill_id)
        recent_rows = get_recent_rows(TABLE, room_id)
    except Exception as exc:
        st.error(f"อ่านข้อมูลจากฐานข้อมูลไม่สำเร็จ: {exc}")
        st.stop()

    prev_elec = to_float(prev_row["prev_elec"]) if prev_row else 0.0
    prev_water = to_float(prev_row["prev_water"]) if prev_row else 0.0

    st.info(f"งวด **{thai_period(period)}**  |  รหัสบิล `{bill_id}`")
    if prev_row:
        st.success(
            f"พบเลขมิเตอร์เดือนก่อน (บิล {prev_row['bill_id']}) → "
            f"ไฟ **{prev_elec:,.0f}**  |  น้ำ **{prev_water:,.1f}**"
        )
    else:
        st.warning(
            "ไม่พบบิลเดือนก่อนของห้องนี้ ระบบเริ่มนับจาก 0 "
            "(พิมพ์เลขมิเตอร์เดือนก่อนเองในฟอร์มได้)"
        )
    if existing:
        st.warning("ห้องนี้มีบิลของงวดนี้อยู่แล้ว หากกดบันทึก ระบบจะแก้ไขทับข้อมูลเดิม")

    with st.expander(f"🔍 ตรวจสอบข้อมูลห้อง {room_id} ในตาราง {TABLE}"):
        if recent_rows:
            dbg = pd.DataFrame(
                [
                    {
                        "bill_id": r["bill_id"],
                        "bill_date": r["bill_date"],
                        "elec_meter": float_or_none(r["elec_meter"]),
                        "water_meter": float_or_none(r["water_meter"]),
                        "elec_meter_end": float_or_none(r["elec_meter_end"]),
                        "water_meter_end": float_or_none(r["water_meter_end"]),
                    }
                    for r in recent_rows
                ]
            )
            st.caption("บิลล่าสุดของห้องนี้ (ข้อมูลเก่าจะมี *_meter_end เป็น 0 ระบบจึงใช้ elec_meter / water_meter แทน)")
            st.dataframe(dbg, use_container_width=True, hide_index=True)
        else:
            st.write("ยังไม่มีบิลของห้องนี้ในตารางนี้เลย")

    # ค่าเริ่มต้นของช่องมิเตอร์ปัจจุบัน: ถ้าเป็นบิลที่มีอยู่แล้วใช้ค่าเดิม ไม่งั้นใช้ค่าเดือนก่อน
    if existing:
        default_elec, default_water = current_meters_of(existing)
    else:
        default_elec, default_water = prev_elec, prev_water

    key_prefix = f"{TABLE}_{bill_id}"
    with st.form("bill_form"):
        rent_default = to_float(existing["rent"]) if existing else rates["rent"]
        rent = st.number_input(
            "ค่าเช่าห้องพัก (บาท)", min_value=0.0, value=float(rent_default),
            step=100.0, format="%.2f", key=f"rent_{key_prefix}",
        )

        pc1, pc2 = st.columns(2)
        with pc1:
            prev_elec_in = st.number_input(
                "มิเตอร์ไฟเดือนก่อน (แก้ไขได้)", min_value=0, value=int(round(prev_elec)),
                step=1, format="%d", key=f"pe_{key_prefix}_{prev_elec}",
            )
        with pc2:
            prev_water_in = st.number_input(
                "มิเตอร์น้ำเดือนก่อน (แก้ไขได้)", min_value=0.0, value=float(prev_water),
                step=1.0, format="%.1f", key=f"pw_{key_prefix}_{prev_water}",
            )

        current_elec_meter = st.number_input(
            "เลขมิเตอร์ไฟฟ้าปัจจุบัน", min_value=0, value=int(round(default_elec)),
            step=1, format="%d", key=f"elec_{key_prefix}",
        )
        current_water_meter = st.number_input(
            "เลขมิเตอร์น้ำปัจจุบัน", min_value=0.0, value=float(default_water),
            step=1.0, format="%.1f", key=f"water_{key_prefix}",
        )
        parking_fee = st.number_input(
            "ค่าที่จอดรถ (บาท) ถ้าไม่มีใส่ 0", min_value=0.0,
            value=float(to_float(existing["parking_fee"])) if existing else 0.0,
            step=50.0, format="%.2f", key=f"parking_{key_prefix}",
        )
        note = st.text_area(
            "หมายเหตุ", value=(existing["note"] or "") if existing else "", key=f"note_{key_prefix}"
        )
        submitted = st.form_submit_button("💾 คำนวณและบันทึก", use_container_width=True)

    if submitted:
        try:
            if current_elec_meter < prev_elec_in:
                st.warning("เลขมิเตอร์ไฟปัจจุบันน้อยกว่าเดือนก่อน ระบบคิดหน่วยที่ใช้เป็น 0 กรุณาตรวจสอบ")
            if current_water_meter < prev_water_in:
                st.warning("เลขมิเตอร์น้ำปัจจุบันน้อยกว่าเดือนก่อน ระบบคิดหน่วยที่ใช้เป็น 0 กรุณาตรวจสอบ")

            elec_unit_used = max(0.0, float(current_elec_meter) - float(prev_elec_in))
            water_unit_used = max(0.0, float(current_water_meter) - float(prev_water_in))
            elec_charge = elec_unit_used * rates["elec_per_unit"]
            water_charge = water_unit_used * rates["water_per_unit"]
            total_charge = rent + elec_charge + water_charge + parking_fee

            save_bill(
                TABLE,
                {
                    "bill_id": bill_id,
                    "room_id": int(room_id),
                    "bill_date": period,
                    "elec_meter": int(current_elec_meter),
                    "water_meter": float(current_water_meter),
                    "note": note.strip() or None,
                    "elec_meter_end": float(current_elec_meter),
                    "water_meter_end": float(current_water_meter),
                    "parking_fee": float(parking_fee),
                    "rent": float(rent),
                    "elec_unit_used": elec_unit_used,
                    "water_unit_used": water_unit_used,
                    "elec_charge": elec_charge,
                    "water_charge": water_charge,
                    "total_charge": total_charge,
                },
            )
            st.success(
                f"บันทึกบิลห้อง {room_id} งวด {thai_period(period)} ลงตาราง {TABLE} เรียบร้อย  "
                f"ยอดรวม {total_charge:,.2f} บาท"
            )
            m1, m2, m3 = st.columns(3)
            m1.metric("ไฟที่ใช้ (หน่วย)", f"{elec_unit_used:,.1f}", f"{elec_charge:,.2f} บาท", delta_color="off")
            m2.metric("น้ำที่ใช้ (หน่วย)", f"{water_unit_used:,.1f}", f"{water_charge:,.2f} บาท", delta_color="off")
            m3.metric("ยอดรวม (บาท)", f"{total_charge:,.2f}")

            # ให้ส่วนใบแจ้งหนี้ด้านล่างเลือกบิลที่เพิ่งบันทึกให้อัตโนมัติ
            st.session_state["rc_room"] = room_id
            st.session_state["rc_bill"] = bill_id
        except Exception as exc:
            st.error(f"บันทึกไม่สำเร็จ: {exc}")

    # -----------------------------------------------------------------------
    # ส่วนใบแจ้งหนี้ (Receipt)
    # -----------------------------------------------------------------------
    st.divider()
    st.subheader("🧾 ใบแจ้งหนี้ (แคปหน้าจอส่งไลน์)")

    if "rc_room" not in st.session_state:
        st.session_state["rc_room"] = room_id

    r1, r2 = st.columns(2)
    with r1:
        rc_room = st.selectbox("เลือกห้องพัก", ROOMS, format_func=lambda r: f"ห้อง {r}", key="rc_room")

    try:
        room_bills = get_bills_of_room(TABLE, rc_room)
    except Exception as exc:
        room_bills = []
        st.error(f"อ่านบิลไม่สำเร็จ: {exc}")

    if not room_bills:
        st.info(f"ห้อง {rc_room} ยังไม่มีบิลในตาราง {TABLE}")
    else:
        bills_by_id = {b["bill_id"]: b for b in room_bills}
        options = list(bills_by_id.keys())
        if st.session_state.get("rc_bill") not in options:
            st.session_state.pop("rc_bill", None)
        with r2:
            rc_bill_id = st.selectbox(
                "เลือกงวดบิล", options,
                format_func=lambda b: thai_period(bills_by_id[b]["bill_date"]),
                key="rc_bill",
            )
        components.html(build_receipt_html(bills_by_id[rc_bill_id]), height=560, scrolling=True)

# ===========================================================================
# Tab 2: ประวัติบิลทั้งหมด
# ===========================================================================
with tab_history:
    st.subheader("ประวัติบิลทั้งหมด")
    st.caption(f"ข้อมูลจากตาราง {TABLE}")
    st.button("🔄 รีเฟรชข้อมูล", key="refresh_history")

    try:
        all_bills = get_all_bills(TABLE)
    except Exception as exc:
        all_bills = []
        st.error(f"อ่านประวัติบิลไม่สำเร็จ: {exc}")

    if not all_bills:
        st.info("ยังไม่มีข้อมูลบิล")
    else:
        df = pd.DataFrame(all_bills)
        df = df.sort_values(["bill_date", "room_id"], ascending=[False, True])
        df["งวด"] = df["bill_date"].apply(thai_period)

        numeric_cols = [
            "rent", "elec_meter", "water_meter", "elec_unit_used", "water_unit_used",
            "elec_charge", "water_charge", "parking_fee", "total_charge",
        ]
        for col in numeric_cols:
            df[col] = df[col].apply(to_float)

        show = df[
            ["bill_id", "room_id", "งวด", "rent", "elec_meter", "water_meter",
             "elec_unit_used", "water_unit_used", "elec_charge", "water_charge",
             "parking_fee", "total_charge", "note"]
        ].rename(
            columns={
                "bill_id": "รหัสบิล",
                "room_id": "ห้อง",
                "rent": "ค่าเช่า",
                "elec_meter": "มิเตอร์ไฟ",
                "water_meter": "มิเตอร์น้ำ",
                "elec_unit_used": "ไฟ (หน่วย)",
                "water_unit_used": "น้ำ (หน่วย)",
                "elec_charge": "ค่าไฟ",
                "water_charge": "ค่าน้ำ",
                "parking_fee": "ที่จอดรถ",
                "total_charge": "ยอดรวม",
                "note": "หมายเหตุ",
            }
        )
        st.dataframe(show, use_container_width=True, hide_index=True)
        st.caption(f"ทั้งหมด {len(show)} รายการ")
