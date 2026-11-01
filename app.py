import datetime
import os
import pandas as pd
import psycopg2
import streamlit as st
import streamlit.components.v1 as components

# ตั้งค่าหน้าจอ App
st.set_page_config(page_title="ระบบจัดการหอพัก", page_icon="🏢", layout="centered")

# ฟังก์ชันเชื่อมต่อ Neon PostgreSQL (รองรับทั้งเครื่องตัวเองและ Render)
def get_db_connection():
    # ตรวจสอบจาก Environment Variable บน Render หรือ st.secrets
    db_url = os.environ.get("DATABASE_URL")
    if not db_url and hasattr(st, "secrets") and "DATABASE_URL" in st.secrets:
        db_url = st.secrets["DATABASE_URL"]
        
    if not db_url:
        st.error("❌ ไม่พบค่า DATABASE_URL กรุณาตั้งค่าใน Environment Variables ของ Render หรือ Streamlit Secrets")
        st.stop()
        
    conn = psycopg2.connect(db_url)
    return conn

st.title("🏢 ระบบจัดการหอพัก (ฉบับจบในที่เดียว)")
st.caption("บันทึกมิเตอร์ คำนวณอัตโนมัติ และออกบิลส่งผู้เช่า")

# กำหนดเรทราคามาตรฐาน
ELEC_RATE = 7.0   # บาทต่อหน่วย
WATER_RATE = 18.0 # บาทต่อหน่วย

tab1, tab2 = st.tabs(["📝 บันทึกมิเตอร์ & ออกใบเสร็จ", "📋 ประวัติบิลทั้งหมด"])

with tab1:
    st.subheader("📝 ฟอร์มบันทึกและคำนวณบิลประจำเดือน")
    
    with st.form("quick_bill_form"):
        col1, col2 = st.columns(2)
        
        with col1:
            room_id = st.selectbox("🚪 เลือกห้องพัก", options=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10], index=0)
            bill_date = st.date_input("📅 ประจำวันที่/งวดเดือน", value=datetime.date.today())
            rent = st.number_input("🏠 ค่าเช่าห้องพัก (บาท)", min_value=0.0, step=100.0, value=3200.0)
            
        with col2:
            current_elec_meter = st.number_input("⚡ เลขมิเตอร์ไฟฟ้าปัจจุบัน (ที่อ่านได้)", min_value=0.0, step=1.0, value=0.0)
            current_water_meter = st.number_input("💧 เลขมิเตอร์น้ำปัจจุบัน (ที่อ่านได้)", min_value=0.0, step=1.0, value=0.0)
            parking_fee = st.number_input("🚗 ค่าที่จอดรถ (ถ้ามี / ถ้าไม่มีใส่ 0)", min_value=0.0, step=50.0, value=0.0)
            
        note = st.text_input("📝 หมายเหตุ (ถ้ามี)", placeholder="เช่น ค่าส่วนกลาง / ย้ายเข้าใหม่")
        
        submitted = st.form_submit_button("🧮 คำนวณและบันทึกข้อมูลทันที")
        
        if submitted:
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                
                # ดึงข้อมูลมิเตอร์ของเดือนก่อนหน้าสำหรับห้องนี้ เพื่อนำมาลบคำนวณหน่วยที่ใช้
                prev_query = """
                    SELECT elec_meter_end, water_meter_end 
                    FROM dorm_bills 
                    WHERE room_id = %s 
                    ORDER BY bill_date DESC LIMIT 1;
                """
                cur.execute(prev_query, (room_id,))
                prev_row = cur.fetchone()
                
                prev_elec = prev_row[0] if prev_row and prev_row[0] is not None else 0.0
                prev_water = prev_row[1] if prev_row and prev_row[1] is not None else 0.0
                
                # คำนวณหน่วยที่ใช้จริง (ถ้าเลขปัจจุบันน้อยกว่าเดือนก่อน ให้ใช้ 0 ป้องกันติดลบ)
                elec_unit_used = max(0.0, current_elec_meter - prev_elec)
                water_unit_used = max(0.0, current_water_meter - prev_water)
                
                elec_charge = elec_unit_used * ELEC_RATE
                water_charge = water_unit_used * WATER_RATE
                total_charge = rent + elec_charge + water_charge + parking_fee
                
                date_str = bill_date.strftime("%Y%m")
                bill_id = f"{date_str}-{room_id}"
                
                # บันทึกลงฐานข้อมูล (รวมถึงเก็บเลขมิเตอร์สิ้นสุดไว้ใช้เทียบเดือนหน้า)
                insert_query = """
                    INSERT INTO dorm_bills (
                        bill_id, room_id, bill_date, rent, 
                        elec_unit_used, water_unit_used, 
                        elec_charge, water_charge, parking_fee, total_charge, note,
                        elec_meter_end, water_meter_end
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (bill_id) 
                    DO UPDATE SET 
                        bill_date = EXCLUDED.bill_date,
                        rent = EXCLUDED.rent,
                        elec_unit_used = EXCLUDED.elec_unit_used,
                        water_unit_used = EXCLUDED.water_unit_used,
                        elec_charge = EXCLUDED.elec_charge,
                        water_charge = EXCLUDED.water_charge,
                        parking_fee = EXCLUDED.parking_fee,
                        total_charge = EXCLUDED.total_charge,
                        note = EXCLUDED.note,
                        elec_meter_end = EXCLUDED.elec_meter_end,
                        water_meter_end = EXCLUDED.water_meter_end;
                """
                cur.execute(insert_query, (
                    bill_id, room_id, bill_date, rent, 
                    elec_unit_used, water_unit_used, 
                    elec_charge, water_charge, parking_fee, total_charge, note,
                    current_elec_meter, current_water_meter
                ))
                conn.commit()
                cur.close()
                conn.close()
                
                st.success(f"✅ บันทึกห้อง {room_id} สำเร็จ! (ใช้ไฟ {elec_unit_used} หน่วย,ใช้น้ำ {water_unit_used} หน่วย | ยอดรวม: {total_charge:,.2f} บาท)")
            except Exception as e:
                st.error(f"❌ เกิดข้อผิดพลาด: {e}")

    st.markdown("---")
    st.subheader("📄 แสดงใบแจ้งหนี้สำหรับแคปเจอร์ส่งผู้เช่า")
    
    try:
        conn = get_db_connection()
        df_bills = pd.read_sql("SELECT * FROM dorm_bills ORDER BY bill_date DESC, room_id ASC", conn)
        conn.close()
        
        if not df_bills.empty:
            col_s1, col_s2 = st.columns(2)
            with col_s1:
                sel_room = st.selectbox("🔍 เลือกห้องที่ต้องการดูบิล", options=df_bills["room_id"].unique(), key="receipt_room")
            with col_s2:
                room_records = df_bills[df_bills["room_id"] == sel_room]
                sel_bill = st.selectbox("📅 เลือกงวดบิล", options=room_records["bill_id"].unique(), key="receipt_bill")
                
            bill_data = room_records[room_records["bill_id"] == sel_bill].iloc[0]
            
            b_date = pd.to_datetime(bill_data['bill_date'])
            thai_months = {1:"มกราคม", 2:"กุมภาพันธ์", 3:"มีนาคม", 4:"เมษายน", 5:"พฤษภาคม", 6:"มิถุนายน", 
                           7:"กรกฎาคม", 8:"สิงหาคม", 9:"กันยายน", 10:"ตุลาคม", 11:"พฤศจิกายน", 12:"ธันวาคม"}
            period_str = f"{thai_months.get(b_date.month, '')} {b_date.year + 543}"
            
            parking_row = ""
            if float(bill_data['parking_fee']) > 0:
                parking_row = f"""
                <div class="row-item">
                    <span>🚗 ค่าที่จอดรถ</span>
                    <b>{float(bill_data['parking_fee']):,.2f} บาท</b>
                </div>
                """

            receipt_html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <style>
                    body {{ font-family: sans-serif; background: #fff; color: #222; margin: 0; padding: 5px; }}
                    .receipt {{
                        border: 3px solid #2E7D32; padding: 20px; border-radius: 15px;
                        background: #fff; max-width: 500px; margin: 0 auto;
                        box-shadow: 0 4px 10px rgba(0,0,0,0.1);
                    }}
                    .head {{ text-align: center; }}
                    .badge {{ background: #f1f8e9; padding: 10px 15px; border-radius: 8px; margin-bottom: 12px; font-size: 16px; }}
                    .row-item {{ display: flex; justify-content: space-between; border-bottom: 1px dashed #eee; padding: 8px 0; font-size: 15px; }}
                    .total {{ background: #e8f5e9; padding: 12px; border-radius: 10px; text-align: center; margin-top: 12px; }}
                </style>
            </head>
            <body>
                <div class="receipt">
                    <div class="head">
                        <h2 style="color: #2E7D32; margin: 0 0 3px 0;">🏢 ใบแจ้งหนี้ค่าเช่าหอพัก</h2>
                        <p style="color: #444; font-size: 15px; margin: 0; font-weight: bold;">ประจำเดือน {period_str}</p>
                    </div>
                    <hr style="border: 1px solid #ddd; margin: 12px 0;">
                    <div class="badge">
                        🏠 <b>ห้องพักหมายเลข:</b> <span style="font-size: 20px; color: #1b5e20; font-weight: bold;">{bill_data['room_id']}</span><br>
                        <span style="font-size: 12px; color: #555;">รหัสบิล: {bill_data['bill_id']}</span>
                    </div>
                    <div>
                        <div class="row-item"><span>🛏️ ค่าเช่าห้องพัก</span><b>{float(bill_data['rent']):,.2f} บาท</b></div>
                        <div class="row-item"><span>💧 ค่าน้ำ ({bill_data['water_unit_used']} หน่วย)</span><b>{float(bill_data['water_charge']):,.2f} บาท</b></div>
                        <div class="row-item"><span>⚡ ค่าไฟ ({bill_data['elec_unit_used']} หน่วย)</span><b>{float(bill_data['elec_charge']):,.2f} บาท</b></div>
                        {parking_row}
                    </div>
                    <div class="total">
                        <p style="margin: 0; font-size: 15px; color: #2e7d32; font-weight: bold;">ยอดรวมสุทธิที่ต้องชำระ</p>
                        <h1 style="margin: 3px 0 0 0; color: #c62828; font-size: 28px;">{float(bill_data['total_charge']):,.2f} บาท</h1>
                    </div>
                    <p style="text-align: center; font-size: 12px; color: #888; margin: 10px 0 0 0;">ขอบคุณที่ใช้บริการค่ะ 🙏</p>
                </div>
            </body>
            </html>
            """
            
            components.html(receipt_html, height=550, scrolling=False)
            st.info("💡 **วิธีใช้งาน:** แคปเจอร์หน้าจอกรอบใบเสร็จด้านบนนี้ส่งเข้า Line ผู้เช่าได้ทันทีครับ!")
        else:
            st.info("ยังไม่มีข้อมูลบิลในระบบ")
    except Exception as e:
        st.error(f"ไม่สามารถโหลดข้อมูลใบเสร็จได้: {e}")

with tab2:
    st.subheader("📋 ประวัติการบันทึกบิลทั้งหมดในระบบ (Neon DB)")
    if st.button("🔄 รีเฟรชข้อมูล"):
        st.rerun()
    try:
        conn = get_db_connection()
        df_all = pd.read_sql("SELECT * FROM dorm_bills ORDER BY bill_date DESC", conn)
        conn.close()
        st.dataframe(df_all, use_container_width=True)
    except Exception as e:
        st.error(f"เกิดข้อผิดพลาด: {e}")
