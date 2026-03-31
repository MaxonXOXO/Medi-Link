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
    
    # Initialize engine with retries
    for attempt in range(3):
        try:
            engine = pyttsx3.init()
            engine.setProperty('rate', 170)
            engine.setProperty('volume', 1.0)
            
            # Set voice (optional - remove if causes issues)
            try:
                voices = engine.getProperty('voices')
                if voices:
                    engine.setProperty('voice', voices[0].id)
            except:
                pass  # Skip voice selection if it fails
            
            # Test the engine with empty speak
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
            
            # Small delay to prevent overlapping speech
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
                
                # Try to reinitialize engine after 2 consecutive errors
                if consecutive_errors >= 2:
                    print("🔄 Reinitializing TTS engine...")
                    try:
                        with _tts_lock:
                            engine.stop()
                    except:
                        pass
                    
                    # Create new engine
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

# Start TTS worker thread (not daemon so it can clean up properly)
_tts_thread = threading.Thread(target=_tts_worker, daemon=False)
_tts_thread.start()
time.sleep(1)  # Give engine time to initialize

def speak_async(text, priority=False):
    """Queue text for speech. priority=True clears any pending items first."""
    if not text:
        return
    
    # Clean up the text (remove action tags)
    clean = text.replace('[ACTION: MEDICINE]', '').replace('[ACTION: DOCTOR]', '').strip()
    if not clean:
        return
    
    if priority:
        # Clear queue for priority messages
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

# Global variable to hold the scanned user ID
last_scanned_uid = None
ser = None

# --- SETUP SERIAL CONNECTION ---
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    time.sleep(2)
    print(f"✅ Connected to ESP32 on {SERIAL_PORT}")
except Exception as e:
    print(f"⚠️ Warning: Could not connect to ESP32 on {SERIAL_PORT}. Running without hardware triggers.")

# --- BACKGROUND THREAD: LISTEN TO ESP32 ---
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

# Start the listener thread
thread = threading.Thread(target=listen_to_esp32, daemon=True)
thread.start()

# --- HELPER FUNCTIONS ---
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

    symptom_string = ", ".join(selected_symptoms)
    full_prompt = f"Patient: {user_name}. History: {user_history}. Symptoms: {symptom_string}."

    try:
        response = ollama.chat(model='llama3.2:1b', messages=[
            {
                'role': 'system',
                'content': (
                    "You are Dr. Medi, a licensed physician working at a smart health kiosk. "
                    "Speak directly and confidently as a real doctor would — never say you are an AI, never add disclaimers, never suggest consulting another doctor unless the action is [ACTION: DOCTOR]. "
                    "Rules: "
                    "1. Check the patient history for allergies before recommending any medication — never suggest a drug they are allergic to. "
                    "2. If symptoms are mild and safe to treat, name the exact OTC medication to take (e.g. Paracetamol 500mg), give a brief instruction, and end your reply with [ACTION: MEDICINE]. "
                    "3. If symptoms are severe (chest pain, breathing difficulty) or there is an allergy risk, give a one-line reason and end with [ACTION: DOCTOR]. "
                    "4. Be concise — maximum 2 sentences. No preamble, no disclaimers, no 'I am not a doctor' language. Write as if you are the doctor."
                )
            },
            {'role': 'user', 'content': full_prompt}
        ])

        reply_text = response['message']['content']

        action = "none"
        if "[ACTION: MEDICINE]" in reply_text:
            action = "medicine"
            if ser:
                ser.write(b'M')
        elif "[ACTION: DOCTOR]" in reply_text:
            action = "doctor"

        # Speak the AI reply
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

@app.route('/dispense_essential', methods=['POST'])
def dispense_essential():
    item = request.json.get('item')
    print(f"🏥 Dispensing Essential: {item}")
    if ser:
        ser.write(b'E')
    return jsonify({"status": "success"})

@app.route('/tts_status', methods=['GET'])
def tts_status():
    """Check TTS system health"""
    return jsonify({
        "status": "ok",
        "queue_size": _tts_queue.qsize()
    })

if __name__ == '__main__':
    # Note: use_reloader=False is important for multi-threading
    app.run(debug=True, port=5000, use_reloader=False)