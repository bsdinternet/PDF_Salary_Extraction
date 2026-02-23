import pdfplumber
import pandas as pd
import re
import os

# ==========================================
# CLASS 1: Extract Global Salary Metadata
# ==========================================
class SalaryMetadataExtractor:
    def __init__(self, pdf_path):
        self.pdf_path = pdf_path
        self.raw_text = ""
        self.data = {
            "Employee Name": "Unknown",
            "Date Range": None,
            "Earned Basic": 0.0,
            "Payable Days": 0,
            "Total Fine Amount": 0.0,
            "Total Fine Mins": 0,
            "Total OT Amount": 0.0,
            "Total OT Mins": 0,
            "Per Day Salary": 0.0,
            "Fine Rate Per Min": 0.0,
            "OT Rate Per Min": 0.0
        }
        self._extract_text()
        self._parse_values()
        self._calculate_rates()
        self.print_summary()

    def _extract_text(self):
        with pdfplumber.open(self.pdf_path) as pdf:
            self.raw_text = "\n".join([page.extract_text() for page in pdf.pages])

    def _parse_time_to_mins(self, token):
        """
        Parse tokens like '19:53Hrs' or '0Mins' (no space between value and unit).
        Returns total minutes as int.
        """
        if not token: return 0
        if 'Mins' in token:
            m = re.search(r'(\d+)', token)
            return int(m.group(1)) if m else 0
        m = re.search(r'(\d+):(\d+)', token)
        return int(m.group(1)) * 60 + int(m.group(2)) if m else 0

    def _parse_values(self):
        t = self.raw_text

        # 1. Employee Name — handles 'EmployeeName: Deepika' (no space)
        name = re.search(r'Employee\s*Name:\s*([A-Za-z]+)', t)
        if name: self.data["Employee Name"] = name.group(1)

        # 2. Date Range — e.g. "26 Dec 25 - 25 Jan 26"
        drange = re.search(r'(\d{1,2}\s[A-Za-z]{3}\s\d{2}\s-\s\d{1,2}\s[A-Za-z]{3}\s\d{2})', t)
        if drange: self.data["Date Range"] = drange.group(1)

        # 3. Payable Days — handles 'PayableDays: 21Days' (no spaces)
        days = re.search(r'Payable\s*Days:\s*(\d+)', t)
        if days: self.data["Payable Days"] = int(days.group(1))

        # 4. Earned Basic — 'BasicPay 11000 8884.62' or 'Basic Pay 8000 5230.77'
        wages = re.search(r'Basic\s*Pay\s+[\d,]+\s+([\d,.]+)', t)
        if wages:
            self.data["Earned Basic"] = float(wages.group(1).replace(',', ''))
        else:
            # Fallback: Total Earnings line
            te = re.search(r'Total\s*Earnings\s+([\d,.]+)', t)
            if te: self.data["Earned Basic"] = float(te.group(1).replace(',', ''))

        # 5. OT Amount (absent = 0)
        ot_amt = re.search(r'Overtime\s+([\d,.]+)', t)
        if ot_amt: self.data["Total OT Amount"] = float(ot_amt.group(1).replace(',', ''))

        # 6. Fine Amount — '\bFine 803.93' or 'Fine 358.2'
        fine_amt = re.search(r'\bFine\s+([\d,.]+)', t)
        if fine_amt: self.data["Total Fine Amount"] = float(fine_amt.group(1).replace(',', ''))

        # 7 & 8. OT Hours + Fine Hours from summary table row
        self._parse_ot_fine_from_summary(t)

    def _parse_ot_fine_from_summary(self, t):
        """
        The PDF summary row (via pdfplumber, spaces stripped) looks like:

          Header: 'PresentDays AbsentDays HalfDays Leaves HoursWorked OTHours TotalFineHours'
          Values: '16 9 0 6 0Mins 19:53Hrs 17:58Hrs'   ← Jaya (has OT)
              OR: '21 5 0 4 0Mins 0Mins 38:12Hrs'       ← Deepika (no OT)

        Tokens have NO space between value and unit: '19:53Hrs', '0Mins', '38:12Hrs'.
        We match last 2 tokens from the values row: second-to-last = OT, last = Fine.
        """
        lines = t.split('\n')
        for i, line in enumerate(lines):
            # Match header with possible no-space variants: 'OTHours' or 'OT Hours'
            if re.search(r'OT\s*Hours', line) and re.search(r'Total\s*Fine\s*Hours', line):
                for j in range(i + 1, min(i + 5, len(lines))):
                    candidate = lines[j].strip()
                    if not candidate:
                        continue
                    # Match tokens: '19:53Hrs', '0Mins', '38:12Hrs' (no space before unit)
                    tokens = re.findall(r'\d+:\d+Hrs|\d+Mins', candidate)
                    if len(tokens) >= 2:
                        ot_mins   = self._parse_time_to_mins(tokens[-2])
                        fine_mins = self._parse_time_to_mins(tokens[-1])
                        # Safety: if OT amount is 0 but ot_mins > 0, tokens misread
                        if self.data["Total OT Amount"] == 0.0 and ot_mins > 0 and fine_mins == 0:
                            fine_mins, ot_mins = ot_mins, 0
                        self.data["Total OT Mins"]   = ot_mins
                        self.data["Total Fine Mins"] = fine_mins
                    break
                break

    def _calculate_rates(self):
        d = self.data
        if d["Payable Days"] > 0:
            d["Per Day Salary"] = d["Earned Basic"] / d["Payable Days"]
        if d["Total Fine Mins"] > 0:
            d["Fine Rate Per Min"] = d["Total Fine Amount"] / d["Total Fine Mins"]
        if d["Total OT Mins"] > 0:
            d["OT Rate Per Min"] = d["Total OT Amount"] / d["Total OT Mins"]

    def print_summary(self):
        print("\n" + "="*50)
        print("SALARY METADATA EXTRACTION")
        print("="*50)
        for k, v in self.data.items():
            if isinstance(v, float):
                print(f"{k:20}: {v:.4f}")
            else:
                print(f"{k:20}: {v}")
        print("="*50 + "\n")


# ==========================================
# CLASS 2: Extract & Process Calendar Data
# ==========================================
class AttendanceExtractor:
    """
    Uses pdfplumber extract_words() with x/y coordinates.
    pdfplumber collapses all 7 calendar columns into single text lines,
    but word-level extraction preserves x positions — so we can accurately
    assign P/OT/F tokens to the correct date column.
    """
    ALL_MONTHS = 'Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec'
    DATE_PAT   = re.compile(r'(\d{1,2})(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)')

    def __init__(self, pdf_path, metadata):
        self.pdf_path = pdf_path
        self.metadata = metadata
        self.calendar_data    = []
        self.final_sheet_data = []

    def _parse_time_to_mins(self, text):
        """Parse '(7:22Hrs)' or '7:22' to minutes."""
        m = re.search(r'(\d+):(\d+)', str(text))
        return int(m.group(1)) * 60 + int(m.group(2)) if m else 0

    def _round_y(self, y, snap=3):
        return round(y / snap) * snap

    def _get_month_pattern(self, master_dates):
        months = list(dict.fromkeys([dt.strftime("%b") for dt in master_dates]))
        return "|".join(months)

    def process(self):
        # 1. Build date map from metadata date range
        start_str, end_str = self.metadata["Date Range"].split(' - ')
        start_dt = pd.to_datetime(start_str, format='%d %b %y')
        end_dt   = pd.to_datetime(end_str,   format='%d %b %y')
        master_dates = pd.date_range(start=start_dt, end=end_dt)
        date_map = {
            dt.strftime("%d %b"): {"Day": dt.strftime("%a"), "P": "", "OT": "", "F": ""}
            for dt in master_dates
        }

        # 2. Extract words with coordinates from page 1
        with pdfplumber.open(self.pdf_path) as pdf:
            all_words = pdf.pages[0].extract_words()

        # 3. Group words into lines by y-coordinate (snap to 3px grid)
        lines_by_y = {}
        for w in all_words:
            yk = self._round_y(w['top'])
            lines_by_y.setdefault(yk, []).append(w)

        # 4. Walk lines top-to-bottom
        in_calendar = False
        current_week_dates = []  # [(date_key, x_center), ...] for current week block

        for yk in sorted(lines_by_y.keys()):
            row = sorted(lines_by_y[yk], key=lambda w: w['x0'])
            text = ' '.join(w['text'] for w in row)

            # Detect start of calendar section
            if 'Monday' in text and 'Tuesday' in text:
                in_calendar = True
                continue
            if not in_calendar:
                continue

            # Detect date header row (e.g. '26Dec General... 27Dec 28Dec')
            date_words = [(self.DATE_PAT.search(w['text']), w)
                          for w in row if self.DATE_PAT.search(w['text'])]
            if date_words:
                current_week_dates = []
                for match, word in date_words:
                    day_num, mon = match.group(1), match.group(2)
                    key = f"{int(day_num):02d} {mon}"
                    x_center = (word['x0'] + word['x1']) / 2
                    current_week_dates.append((key, x_center))
                continue

            # P / OT / F data row — assign each token to nearest date in current week
            def nearest_date(x_val):
                if not current_week_dates:
                    return None
                return min(current_week_dates, key=lambda item: abs(item[1] - x_val))[0]

            i = 0
            while i < len(row):
                w = row[i]
                token = w['text']

                if token in ('P', 'OT', 'F') and i + 1 < len(row) and row[i+1]['text'].startswith('('):
                    # e.g.  P  (7:22Hrs)
                    time_text = row[i + 1]['text']
                    dk = nearest_date(w['x0'])
                    if dk and dk in date_map:
                        date_map[dk][token] = time_text
                    i += 2

                elif token in ('Absent', 'WeeklyOff', 'PaidHoliday'):
                    dk = nearest_date(w['x0'])
                    if dk and dk in date_map and not date_map[dk]['P']:
                        clean = (token
                                 .replace('WeeklyOff',   'Weekly Off')
                                 .replace('PaidHoliday', 'Paid Holiday'))
                        date_map[dk]['P'] = clean
                    i += 1

                else:
                    i += 1

        # 5. Calculate per-day pay
        for idx, (d_str, vals) in enumerate(date_map.items(), 1):
            p_status = vals["P"]

            if "Absent" in p_status or "Weekly Off" in p_status:
                base = 0.0
            elif "Paid Holiday" in p_status:
                base = self.metadata["Per Day Salary"]
            elif vals["Day"] == "Sun" and not p_status:
                base = 0.0  # Sunday with no entry = unpaid
            else:
                base = self.metadata["Per Day Salary"]

            ot_v   = self._parse_time_to_mins(vals["OT"]) * self.metadata["OT Rate Per Min"]
            fine_v = self._parse_time_to_mins(vals["F"])  * self.metadata["Fine Rate Per Min"]
            net    = base + ot_v - fine_v

            self.calendar_data.append({
                "Index": idx,
                "Date":  d_str,
                "Day":   vals["Day"],
                "P":     vals["P"],
                "OT":    vals["OT"],
                "F":     vals["F"]
            })
            self.final_sheet_data.append({
                "Date":        d_str,
                "Day":         vals["Day"],
                "Net-Per-Day": round(net, 2)
            })

    def get_dataframes(self):
        return pd.DataFrame(self.calendar_data), pd.DataFrame(self.final_sheet_data)


# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":
    BASE_PATH = r"C:\Users\deepa_gzfno2a\Downloads\Attendance automation\SalarySlip_Deepika_1769536323098.pdf"

    pdf_name     = os.path.splitext(os.path.basename(BASE_PATH))[0]
    OUTPUT_EXCEL = f"6789{pdf_name}_Salary.xlsx"

    meta = SalaryMetadataExtractor(BASE_PATH)
    proc = AttendanceExtractor(BASE_PATH, meta.data)
    proc.process()
    df_t, df_n = proc.get_dataframes()

    with pd.ExcelWriter(OUTPUT_EXCEL) as writer:
        df_n.to_excel(writer, sheet_name="PerDaySalary", index=False)
        df_t.to_excel(writer, sheet_name="Timings", index=False)

    print(f"\nExcel Generated: {OUTPUT_EXCEL}")
    print(f"Earned Basic (from PDF) : {meta.data['Earned Basic']:.2f}")
    print(f"Net Pay (calc total)    : {df_n['Net-Per-Day'].sum():.2f}")
    print(f"Expected Net Pay        : {meta.data['Earned Basic'] - meta.data['Total Fine Amount'] + meta.data['Total OT Amount']:.2f}")