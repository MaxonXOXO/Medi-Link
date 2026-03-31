from flask import Flask, render_template, request, jsonify
import ollama
import serial
import json
import threading
import time
import pyttsx3
import queue
import sys

app = Flask(__name__)

# --- TTS SETUP with pyttsx3 (Fixed for Python 3.14) ---
_tts_queue = queue.Queue()
_tts_lock = threading.Lock()

def _tts_worker():
    """Single long-lived thread that owns the pyttsx3 engine."""
    engine = None
    
    for attempt in range(3):
        try:
            engine = pyttsx3.init()
            engine.setProperty('rate', 170)
            engine.setProperty('volume', 1.0)
            try:
                voices = engine.getProperty('voices')
                if voices:
                    engine.setProperty('voice', voices[0].id)
            except:
                pass
            engine.say("")
            engine.runAndWait()
            print("✅ TTS engine initialized successfully")
            break
        except Exception as e:
            print(f"⚠️ TTS init attempt {attempt + 1} failed: {e}")
            time.sleep(1)
            if attempt == 2:
                print("❌ Failed to initialize TTS engine after 3 attempts")
                return
    
    if engine is None:
        return
    
    consecutive_errors = 0
    
    while True:
        try:
            text = _tts_queue.get(timeout=0.5)
            if not text:
                _tts_queue.task_done()
                continue
            
            time.sleep(0.2)
            
            try:
                with _tts_lock:
                    engine.say(text)
                    engine.runAndWait()
                consecutive_errors = 0
                print(f"🔊 Spoke: {text[:50]}...")
                
            except Exception as e:
                consecutive_errors += 1
                print(f"⚠️ TTS error (attempt {consecutive_errors}): {e}")
                
                if consecutive_errors >= 2:
                    print("🔄 Reinitializing TTS engine...")
                    try:
                        with _tts_lock:
                            engine.stop()
                    except:
                        pass
                    try:
                        new_engine = pyttsx3.init()
                        new_engine.setProperty('rate', 170)
                        new_engine.setProperty('volume', 1.0)
                        with _tts_lock:
                            engine = new_engine
                        consecutive_errors = 0
                        print("✅ TTS engine reinitialized")
                    except Exception as reinit_err:
                        print(f"❌ Failed to reinitialize: {reinit_err}")
                        time.sleep(2)
            
            _tts_queue.task_done()
            
        except queue.Empty:
            continue
        except Exception as e:
            print(f"❌ TTS worker error: {e}")
            time.sleep(1)

_tts_thread = threading.Thread(target=_tts_worker, daemon=False)
_tts_thread.start()
time.sleep(1)

def speak_async(text, priority=False):
    """Queue text for speech. priority=True clears any pending items first."""
    if not text:
        return
    clean = text.replace('[ACTION: MEDICINE]', '').replace('[ACTION: DOCTOR]', '').strip()
    if not clean:
        return
    if priority:
        cleared = 0
        while not _tts_queue.empty():
            try:
                _tts_queue.get_nowait()
                _tts_queue.task_done()
                cleared += 1
            except queue.Empty:
                break
        if cleared:
            print(f"🗑️ Cleared {cleared} pending TTS messages")
    _tts_queue.put(clean)
    print(f"📝 Queued: {clean[:50]}...")

# --- CONFIGURATION ---
SERIAL_PORT = 'COM3'  # <--- CHANGE THIS TO YOUR ESP32 PORT
BAUD_RATE = 115200

last_scanned_uid = None
ser = None

try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    time.sleep(2)
    print(f"✅ Connected to ESP32 on {SERIAL_PORT}")
except Exception as e:
    print(f"⚠️ Warning: Could not connect to ESP32 on {SERIAL_PORT}. Running without hardware triggers.")

def listen_to_esp32():
    global last_scanned_uid
    while True:
        if ser and ser.in_waiting > 0:
            try:
                line = ser.readline().decode('utf-8').strip()
                print(f"[ESP32 RAW]: '{line}'")
                if "USER:" in line:
                    parts = line.split(":")
                    if len(parts) > 1:
                        last_scanned_uid = parts[1].strip()
                        print(f"✅ Recognized UID: {last_scanned_uid}")
            except Exception as e:
                print(f"❌ Serial Error: {e}")
        time.sleep(0.1)

thread = threading.Thread(target=listen_to_esp32, daemon=True)
thread.start()

def get_user(uid):
    try:
        with open('data.json') as f:
            db = json.load(f)
        print(f"🔍 Looking up UID: {uid}")
        user = db.get(str(uid), None)
        if user is None:
            print(f"❌ UID '{uid}' not found in database!")
            return {"name": "Guest", "history": "Unknown ID"}
        return user
    except Exception as e:
        print(f"❌ Database Error: {e}")
        return {"name": "Error", "history": "Could not read database."}

# --- FLASK ROUTES ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/check_scan', methods=['GET'])
def check_scan():
    global last_scanned_uid
    if last_scanned_uid:
        uid_to_lookup = last_scanned_uid
        last_scanned_uid = None
        user_data = get_user(uid_to_lookup)
        print(f"📤 Sending to UI: {user_data}")
        return jsonify(user_data)
    return jsonify({"status": "waiting"})

@app.route('/ask_ai', methods=['POST'])
def ask_ai():
    data = request.json
    user_name = data.get('name', 'Guest')
    user_history = data.get('history', 'No prior history')
    selected_symptoms = data.get('symptoms', [])
    vitals = data.get('vitals', {})

    symptom_string = ", ".join(selected_symptoms)

    # Build vitals string — only include values that were provided
    vitals_parts = []
    if vitals.get('blood_pressure'): vitals_parts.append(f"BP: {vitals['blood_pressure']}")
    if vitals.get('temperature'):    vitals_parts.append(f"Temp: {vitals['temperature']}")
    if vitals.get('blood_sugar'):    vitals_parts.append(f"Blood sugar: {vitals['blood_sugar']}")
    if vitals.get('weight'):         vitals_parts.append(f"Weight: {vitals['weight']}")
    vitals_string = ", ".join(vitals_parts) if vitals_parts else "Not provided"

    full_prompt = (
        f"Patient: {user_name}. "
        f"Medical history: {user_history}. "
        f"Vitals — {vitals_string}. "
        f"Symptoms: {symptom_string}."
    )

    try:
        response = ollama.chat(model='llama3.2:1b', messages=[
            {
                'role': 'system',
                'content': (
                    "You are Dr. Medi, a licensed physician working at a smart health kiosk. "
                    "Speak directly and confidently as a real doctor would — never say you are an AI, never add disclaimers, never suggest consulting another doctor unless the action is [ACTION: DOCTOR]. "
                    "Rules: "
                    "1. Check the patient history for allergies before recommending any medication — never suggest a drug they are allergic to. "
                    "2. Analyse the vitals if provided — flag abnormal values (e.g. high BP >140/90, high temp >38°C, high blood sugar >140 mg/dL). "
                    "3. If symptoms are mild and safe to treat, name the exact OTC medication to take (e.g. Paracetamol 500mg), give a brief instruction, and end your reply with [ACTION: MEDICINE]. "
                    "4. If symptoms are severe (chest pain, breathing difficulty), vitals are critically abnormal, or there is an allergy risk, give a one-line reason and end with [ACTION: DOCTOR]. "
                    "5. Be concise — maximum 3 sentences. No preamble, no disclaimers. Write as if you are the doctor."
                )
            },
            {'role': 'user', 'content': full_prompt}
        ])

        reply_text = response['message']['content']
        
        # Convert to lowercase to make matching easier and catch AI formatting mistakes
        reply_lower = reply_text.lower()
        
        action = "none"
        
        # Check for the medicine tag OR common medicine names
        if "action: medicine" in reply_lower or "paracetamol" in reply_lower or "ibuprofen" in reply_lower:
            action = "medicine"
            if ser:
                ser.write(b'M')   # Trigger medicine relay (GPIO 19)
                
        # FIXED INDENTATION: This must align with the 'if' above!
        elif "action: doctor" in reply_lower or "doctor" in reply_lower or "hospital" in reply_lower:
            action = "doctor"
            if ser:
                ser.write(b'P4')   # Trigger Doctor referral 

        # Speak the AI reply (priority clears any queued greeting)
        speak_async(reply_text, priority=True)

        return jsonify({"reply": reply_text, "action": action})

    except Exception as e:
        print(f"❌ Ollama Error: {e}")
        return jsonify({"reply": "AI is offline. Please check Ollama.", "action": "none"})

@app.route('/speak', methods=['POST'])
def speak():
    """Generic TTS endpoint for greetings, messages, etc."""
    text = request.json.get('text', '')
    if text:
        speak_async(text)
    return jsonify({"status": "ok"})

@app.route('/register_patient', methods=['POST'])
def register_patient():
    """Write a new patient record into data.json."""
    data = request.json
    name    = data.get('name', '').strip()
    history = data.get('history', 'No known conditions').strip()
    fp_id   = str(data.get('fingerprint_id', '')).strip()

    if not name or not fp_id:
        return jsonify({"status": "error", "error": "Name and fingerprint ID are required."}), 400

    try:
        # Load existing database (or start fresh)
        try:
            with open('data.json', 'r') as f:
                db = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            db = {}

        if fp_id in db:
            return jsonify({"status": "error", "error": f"Fingerprint ID {fp_id} is already registered to {db[fp_id]['name']}."}), 409

        db[fp_id] = {"name": name, "history": history}

        with open('data.json', 'w') as f:
            json.dump(db, f, indent=2)

        print(f"✅ Registered new patient: {name} (ID: {fp_id})")
        return jsonify({"status": "ok"})

    except Exception as e:
        print(f"❌ Registration error: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/reset_kiosk', methods=['POST'])
def reset_kiosk():
    """Called when the New Patient button is pressed — plays 002.mp3 on DFPlayer."""
    if ser:
        ser.write(b'P2')  # DFPlayer: play 002.mp3
    return jsonify({"status": "ok"})

@app.route('/dispense_essential', methods=['POST'])
def dispense_essential():
    item = request.json.get('item')
    print(f"🏥 Dispensing Essential: {item}")
    if ser:
        ser.write(b'E')  # Trigger essentials relay (GPIO 18)
    return jsonify({"status": "success"})

@app.route('/tts_status', methods=['GET'])
def tts_status():
    """Check TTS system health"""
    return jsonify({
        "status": "ok",
        "queue_size": _tts_queue.qsize()
    })

if __name__ == '__main__':
    # use_reloader=False is critical — reloader spawns a second process
    # which starts a second TTS thread and breaks pyttsx3
    app.run(debug=True, port=5000, use_reloader=False)