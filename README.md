🎓 JKLU AI Exam & Seating Scheduler

Hey there! 👋 If you've ever tried manually scheduling university exams, handling student course overlaps, assigning rooms, and making sure best friends taking the same exam aren't sitting right next to each other—you already know what an absolute nightmare it is.
So we are actually a participating in a hackathon our teammates are :-
Akshra Khandelwal (Team leader)
Tanisha Agarwal (Teammate)
Ronak Sethi (Teammate)
Vishal Jogu (Teammate)
hope you learnt something new from our project. Happy coding😊
we built this app to solve exactly that. It's an automated, AI-assisted exam timetabling and seating arrangement system tailored for JK Lakshmipat University (JKLU), packed with a smart layout engine, natural language assistant, and clean PDF/Excel report generators.

🚀 What Can It Do?

Clash-Free Timetabling: Automatically groups courses into exam slots without letting students get double-booked.

Anti-Collusion Seating Engine: Uses a hill-climbing optimization algorithm to ensure students taking the same course aren't seated side-by-side or directly front-to-back. Supports both curved amphitheatre layouts and standard straight rows.

Autonomous Chat Agent: Tired of clicking around? Just type natural commands like:

"Move Monday to Wednesday"

"Cancel Tuesday and auto-reschedule"

"Dr. Mehta is on leave"

"Where does 2026BTech005 sit?"

"Verify constraints"

Role-Based Access Control (RBAC): Separate views for Admin (full control & approval workflow), Faculty (check invigilation duties), and Students (lookup their exam seat).

Self-Healing & Validation: The agent checks its own work against strict constraints before committing any changes. If something breaks a rule, it automatically rolls back.

Print-Ready Reports: Generates professional PDF room seating diagrams and master attendance sheets using ReportLab, plus full multi-sheet Excel exports via OpenPyXL.

🛠️ Tech Stack

Frontend & UI: Streamlit (with custom dark-mode styling)

Data Processing: Pandas, NumPy

PDF Generation: ReportLab

Excel Reports: OpenPyXL

AI / NLP Agent: Rule-based fallback parser + optional LLM integration (Anthropic Claude / Google Gemini API)

⚙️ How to Run Locally

Clone or download the project files (make sure you have app.py).

Install the required dependencies:

pip install streamlit pandas reportlab openpyxl requests



Run the Streamlit app:

streamlit run app.py



Login as Admin:

Open the sidebar and select Admin.

Enter the default demo PIN: admin123 (or set your own via the ADMIN_PIN environment variable).

Toggle "Quick-Load Demo Dataset" to instantly test with 120+ mock students, rooms, and faculty, or paste your own CSV-like data!

📂 Project Structure

├── app.py              # The entire core app (Single-file architecture)
└── README.md           # You are reading this right now!



💡 Notes & Tips

By default, the chat assistant uses a robust rule-based parser so it works completely offline without needing an API key.

If you want multi-step advanced reasoning, you can plug in your Gemini or Anthropic API key right inside the sidebar under the Agent Engine section.

Publishing the timetable locks the draft state and unlocks portal views and PDF downloads.

Feel free to fork, tweak, or raise an issue if you find any bugs! Drop a ⭐ if this saved you from exam scheduling headaches.
