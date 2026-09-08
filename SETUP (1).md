# ReviewSniper — Lead Hunter Setup (Step 1)

## 1. Apne machine par folder banao
Windows pe (tumhari path mein space hai, quotes zaroor lagana):
```
cd "D:\RAYAN COMPUTERs\reviewsniper\scraper"
```

## 2. Python virtual environment banao
```
python -m venv venv
venv\Scripts\activate        # Windows
# ya: source venv/bin/activate   # Mac/Linux
```

## 3. Dependencies install karo
```
pip install -r requirements.txt
playwright install chromium
```

## 4. MongoDB Atlas (Free Tier) set up karo
1. https://www.mongodb.com/cloud/atlas/register par free account banao (no credit card).
2. "Build a Database" -> **M0 Free** cluster choose karo.
3. Database user banao (username + password) — password kahin save kar lo.
4. Network Access mein "Allow Access from Anywhere" (0.0.0.0/0) add karo — GitHub Actions ke liye zaroori hai.
5. "Connect" -> "Drivers" -> connection string copy karo, kuch is tarah dikhega:
   ```
   mongodb+srv://myuser:<password>@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
   ```
6. `<password>` ko apne actual password se replace karo.

## 5. .env file banao
`.env.example` ko copy kar ke `.env` naam do, phir apna MONGODB_URI daalo:
```
copy .env.example .env
```
Phir `.env` file kholo aur MONGODB_URI paste karo.

## 6. Test run karo
```
python lead_hunter.py --query "Dental Clinics in Dubai" --max-listings 10
```

Terminal mein tumhe kuch is tarah output dikhega:
```
[INFO] Searching Google Maps: Dental Clinics in Dubai
[INFO] Found 10 listings to check.
[SAVED] Bright Smile Dental (1★, 6.0h ago)
[SKIP] Already saved: XYZ Clinic
[DONE] 1 crisis lead(s) saved out of 10 checked.
```

## 7. MongoDB Atlas mein data verify karo
Atlas dashboard -> "Browse Collections" -> `reviewsniper` database -> `leads` collection mein saved leads dikhengi.

---

## ⚠️ Important honest notes (must-read)

1. **Google Maps ka DOM structure baar baar change hota hai.** Ye script aaj
   (Sep 2026) ke Maps layout ke hisab se likhi hai — selectors (`role="feed"`,
   `data-review-id`, etc.) kal break ho sakte hain agar Google apna UI update
   kare. Jab script kaam karna band kar de, sabse pehle browser mein manually
   Maps khol kar dekho ke element structure change to nahi hua.

2. **Email is script mein nahi milta.** Google Maps email nahi deta — sirf
   phone aur website link milta hai. Email ke liye Step 2 mein ek alag script
   banayenge jo `website` field ko visit kar ke Contact page se email dhoondega
   (ye bhi 100% reliable nahi hoga — kai businesses email publicly nahi dikhatin).

3. **Rate limiting / blocking ka risk hai.** Agar tum bohat zyada requests
   thore time mein bhejo (khaas kar GitHub Actions ke shared IP se), Google
   temporarily CAPTCHA dikha sakta hai ya block kar sakta hai. `time.sleep(1)`
   already daala hai politeness ke liye — agar block hone lage to ye delay
   badhana ya residential proxy lagana paray ga (jo free nahi hoga).

4. **Ye Google's Terms of Service ke against hai.** Maps scraping automated
   tarah se unki ToS violate karti hai. Legal risk low hai chhoti scale par,
   lekin agar business scale kare to ye cheez tumhe SaaS providers (Vercel,
   MongoDB) ke apne ToS se bhi takra sakti hai agar report ho. Isko dhyan mein
   rakhna.

5. **Pehle test run manually apne laptop par karo**, GitHub Actions automation
   pe jaane se pehle — taake pata chale selectors sahi kaam kar rahe hain
   tumhare region/account ke Google Maps version ke sath.

---

## Next Steps (jab ye kaam kar jaye)
- Step 2: Website se email scrape karne wali script
- Step 3: GitHub Actions workflow (har 6 ghante auto-run)
- Step 4: MERN dashboard jo `leads` collection dikhaye
