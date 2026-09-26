@echo off
rem Double-click this file to set up and run the Gold AI Signal Bot on Windows.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto nopython

if not exist venv\Scripts\python.exe python -m venv venv
venv\Scripts\python -c "import telegram, google.genai, dotenv, socksio, matplotlib" >nul 2>nul
if errorlevel 1 venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 goto failed

if exist .env goto run
echo.
echo Pehli baar: apni 3 keys paste karein (right-click = paste).
echo Ye sirf aapke PC par .env file mein save hongi, GitHub par nahi jayengi.
echo.
set /p TG=Telegram bot token (BotFather se): 
set /p GM=Gemini API key (aistudio.google.com se): 
set /p TD=Twelve Data API key (twelvedata.com se): 
set /p GX=Aur Gemini keys (optional, comma se alag, warna Enter): 
> .env echo TELEGRAM_BOT_TOKEN=%TG%
>> .env echo GEMINI_API_KEY=%GM%
>> .env echo TWELVEDATA_API_KEY=%TD%
>> .env echo GEMINI_API_KEYS=%GX%
echo Keys save ho gayin.

:run
echo.
echo Bot chal raha hai... band karne ke liye ye window band kar dein.
venv\Scripts\python bot.py
pause
exit /b

:nopython
echo Python nahi mila. https://www.python.org/downloads/ se install karein
echo aur install karte waqt "Add python.exe to PATH" zaroor tick karein.
pause
exit /b 1

:failed
echo Setup fail ho gaya. Upar wale error ka screenshot bhej dein.
pause
exit /b 1
