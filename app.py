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
ROOMS = [str(i) for i in range(1, 11)]  # ห้อง 1 - 10 (แก้จำนวนห้องตรงนี้)
DEFAULT_RATES = {"elec_per_unit": 8.0, "water_per_unit": 18.0, "rent": 3000.0}


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


def money(value):
    return f"{to_float(value):,.2f}"


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


def ensure_tables():
    """สร้างตารางให้ถ้ายังไม่มี (ถ้ามีอยู่แล้วจะไม่ทำอะไร)"""
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
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS dorm_bills (
            bill_id          TEXT PRIMARY KEY,
            room_id          TEXT NOT NULL,
            bill_date       DATE NOT NULL,
            rent             NUMERIC(10,2) NOT NULL DEFAULT 0,
            elec_meter_end   NUMERIC(12,2) NOT NULL DEFAULT 0,
            water_meter_end  NUMERIC(12,2) NOT NULL DEFAULT 0,
            elec_unit_used   NUMERIC(12,2) NOT NULL DEFAULT 0,
            water_unit_used  NUMERIC(12,2) NOT NULL DEFAULT 0,
            elec_charge      NUMERIC(12,2) NOT NULL DEFAULT 0,
            water_charge     NUMERIC(12,2) NOT NULL DEFAULT 0,
            parking_fee      NUMERIC(10,2) NOT NULL DEFAULT 0,
            total_charge     NUMERIC(12,2) NOT NULL DEFAULT 0,
            note             TEXT
        )
        """
    )


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
# 3) ฟังก์ชันอ่าน/เขียนข้อมูลบิล
# ---------------------------------------------------------------------------
def get_prev_meters(room_id, period):
    """เลขมิเตอร์สิ้นสุดของงวดก่อนหน้า ถ้าไม่มีให้เริ่มที่ 0"""
    rows = db_fetch(
        """
        SELECT elec_meter_end, water_meter_end
        FROM dorm_bills
        WHERE room_id = %s AND bill_date < %s
        ORDER BY bill_date DESC
        LIMIT 1
        """,
        (room_id, period),
    )
    if not rows:
        return 0.0, 0.0, False
    return to_float(rows[0]["elec_meter_end"]), to_float(rows[0]["water_meter_end"]), True


def get_bill(bill_id):
    rows = db_fetch("SELECT * FROM dorm_bills WHERE bill_id = %s", (bill_id,))
    return rows[0] if rows else None


def get_bills_of_room(room_id):
    return db_fetch(
        "SELECT * FROM dorm_bills WHERE room_id = %s ORDER BY bill_date DESC",
        (room_id,),
    )


def get_all_bills():
    return db_fetch("SELECT * FROM dorm_bills ORDER BY bill_date DESC")


def save_bill(bill):
    """บันทึกบิล ถ้า bill_id ซ้ำจะ UPDATE ทับข้อมูลเดิม"""
    db_execute(
        """
        INSERT INTO dorm_bills (
            bill_id, room_id, bill_date, rent,
            elec_meter_end, water_meter_end,
            elec_unit_used, water_unit_used,
            elec_charge, water_charge,
            parking_fee, total_charge, note
        ) VALUES (
            %(bill_id)s, %(room_id)s, %(bill_date)s, %(rent)s,
            %(elec_meter_end)s, %(water_meter_end)s,
            %(elec_unit_used)s, %(water_unit_used)s,
            %(elec_charge)s, %(water_charge)s,
            %(parking_fee)s, %(total_charge)s, %(note)s
        )
        ON CONFLICT (bill_id) DO UPDATE SET
            room_id         = EXCLUDED.room_id,
            bill_date      = EXCLUDED.bill_date,
            rent            = EXCLUDED.rent,
            elec_meter_end  = EXCLUDED.elec_meter_end,
            water_meter_end = EXCLUDED.water_meter_end,
            elec_unit_used  = EXCLUDED.elec_unit_used,
            water_unit_used = EXCLUDED.water_unit_used,
            elec_charge     = EXCLUDED.elec_charge,
            water_charge    = EXCLUDED.water_charge,
            parking_fee     = EXCLUDED.parking_fee,
            total_charge    = EXCLUDED.total_charge,
            note            = EXCLUDED.note
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
    period_date = bill["bill_date"]
    period_text = thai_period(period_date)
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

try:
    if not st.session_state.get("tables_ready"):
        ensure_tables()
        st.session_state["tables_ready"] = True
    rates, rates_found = get_current_rates()
except Exception as exc:
    st.error(f"เชื่อมต่อฐานข้อมูลไม่สำเร็จ: {exc}")
    st.stop()

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
    bill_id = f"{room_id}-{period:%Y%m}"

    try:
        prev_elec, prev_water, has_prev = get_prev_meters(room_id, period)
        existing = get_bill(bill_id)
    except Exception as exc:
        st.error(f"อ่านข้อมูลจากฐานข้อมูลไม่สำเร็จ: {exc}")
        st.stop()

    st.info(
        f"งวด **{thai_period(period)}**  |  รหัสบิล `{bill_id}`\n\n"
        f"เลขมิเตอร์เดือนก่อน → ไฟ: **{prev_elec:,.1f}**  |  น้ำ: **{prev_water:,.1f}**"
        + ("" if has_prev else "  _(ไม่มีข้อมูลเดือนก่อน เริ่มนับจาก 0)_")
    )
    if existing:
        st.warning("ห้องนี้มีบิลของงวดนี้อยู่แล้ว หากกดบันทึก ระบบจะแก้ไขทับข้อมูลเดิม")

    with st.form("bill_form"):
        rent_default = to_float(existing["rent"]) if existing else rates["rent"]
        rent = st.number_input(
            "ค่าเช่าห้องพัก (บาท)", min_value=0.0, value=float(rent_default),
            step=100.0, format="%.2f", key=f"rent_{bill_id}",
        )
        current_elec_meter = st.number_input(
            "เลขมิเตอร์ไฟฟ้าปัจจุบัน", min_value=0.0,
            value=float(to_float(existing["elec_meter_end"])) if existing else float(prev_elec),
            step=1.0, format="%.1f", key=f"elec_{bill_id}",
        )
        current_water_meter = st.number_input(
            "เลขมิเตอร์น้ำปัจจุบัน", min_value=0.0,
            value=float(to_float(existing["water_meter_end"])) if existing else float(prev_water),
            step=1.0, format="%.1f", key=f"water_{bill_id}",
        )
        parking_fee = st.number_input(
            "ค่าที่จอดรถ (บาท) ถ้าไม่มีใส่ 0", min_value=0.0,
            value=float(to_float(existing["parking_fee"])) if existing else 0.0,
            step=50.0, format="%.2f", key=f"parking_{bill_id}",
        )
        note = st.text_area(
            "หมายเหตุ", value=(existing["note"] or "") if existing else "", key=f"note_{bill_id}"
        )
        submitted = st.form_submit_button("💾 คำนวณและบันทึก", use_container_width=True)

    if submitted:
        try:
            # เลขมิเตอร์เดือนก่อน (ดึงใหม่อีกครั้งตอนกดบันทึกเพื่อความถูกต้อง)
            prev_elec, prev_water, _ = get_prev_meters(room_id, period)

            if current_elec_meter < prev_elec:
                st.warning("เลขมิเตอร์ไฟปัจจุบันน้อยกว่าเดือนก่อน ระบบคิดหน่วยที่ใช้เป็น 0 กรุณาตรวจสอบ")
            if current_water_meter < prev_water:
                st.warning("เลขมิเตอร์น้ำปัจจุบันน้อยกว่าเดือนก่อน ระบบคิดหน่วยที่ใช้เป็น 0 กรุณาตรวจสอบ")

            elec_unit_used = max(0.0, current_elec_meter - prev_elec)
            water_unit_used = max(0.0, current_water_meter - prev_water)
            elec_charge = elec_unit_used * rates["elec_per_unit"]
            water_charge = water_unit_used * rates["water_per_unit"]
            total_charge = rent + elec_charge + water_charge + parking_fee

            save_bill(
                {
                    "bill_id": bill_id,
                    "room_id": room_id,
                    "bill_date": period,
                    "rent": rent,
                    "elec_meter_end": current_elec_meter,
                    "water_meter_end": current_water_meter,
                    "elec_unit_used": elec_unit_used,
                    "water_unit_used": water_unit_used,
                    "elec_charge": elec_charge,
                    "water_charge": water_charge,
                    "parking_fee": parking_fee,
                    "total_charge": total_charge,
                    "note": note.strip() or None,
                }
            )
            st.success(
                f"บันทึกบิลห้อง {room_id} งวด {thai_period(period)} เรียบร้อย  "
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
        room_bills = get_bills_of_room(rc_room)
    except Exception as exc:
        room_bills = []
        st.error(f"อ่านบิลไม่สำเร็จ: {exc}")

    if not room_bills:
        st.info(f"ห้อง {rc_room} ยังไม่มีบิลในระบบ")
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
    st.button("🔄 รีเฟรชข้อมูล", key="refresh_history")

    try:
        all_bills = get_all_bills()
    except Exception as exc:
        all_bills = []
        st.error(f"อ่านประวัติบิลไม่สำเร็จ: {exc}")

    if not all_bills:
        st.info("ยังไม่มีข้อมูลบิล")
    else:
        df = pd.DataFrame(all_bills)
        df["room_sort"] = pd.to_numeric(df["room_id"], errors="coerce")
        df = df.sort_values(["bill_date", "room_sort"], ascending=[False, True])
        df["งวด"] = df["bill_date"].apply(thai_period)

        numeric_cols = [
            "rent", "elec_meter_end", "water_meter_end", "elec_unit_used", "water_unit_used",
            "elec_charge", "water_charge", "parking_fee", "total_charge",
        ]
        for col in numeric_cols:
            df[col] = df[col].apply(to_float)

        show = df[
            ["bill_id", "room_id", "งวด", "rent", "elec_meter_end", "water_meter_end",
             "elec_unit_used", "water_unit_used", "elec_charge", "water_charge",
             "parking_fee", "total_charge", "note"]
        ].rename(
            columns={
                "bill_id": "รหัสบิล",
                "room_id": "ห้อง",
                "rent": "ค่าเช่า",
                "elec_meter_end": "มิเตอร์ไฟ",
                "water_meter_end": "มิเตอร์น้ำ",
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
