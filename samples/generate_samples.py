"""Generate synthetic demo charts.

Every patient, provider, and identifier here is fabricated. Nothing in this
directory is real PHI, and nothing real should ever be committed here.

Produces:
  01_office_visit_digital.pdf   - clean text-layer PDF (the easy case)
  02_inpatient_scanned.pdf      - image-only, slightly rotated and speckled,
                                  to exercise the OCR fallback path
  03_procedure_note_digital.pdf - triggers an NCCI bundling conflict
"""
from __future__ import annotations

import random
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent

OFFICE_NOTE = """RIVERBEND FAMILY MEDICINE
1420 Mill Creek Road, Suite 200

PROGRESS NOTE

Patient ID: RB-884213
Age: 58    Sex: Male
Date of Service: 03/14/2026
Place of Service: 11
Payer: Meridian Health Plan
Provider: Alan T. Whitfield, MD    NPI: 1932847561

CHIEF COMPLAINT:
Follow-up of diabetes and knee pain.

HISTORY OF PRESENT ILLNESS:
Established patient returns for routine follow-up. He reports his home glucose
readings have been running in the 180s to 220s over the past two months. He has
been taking metformin 1000 mg twice daily and states he has not missed doses.
He describes ongoing burning and numbness in both feet in a stocking-glove
distribution, worse at night, which has progressed since his last visit.

He also reports persistent right knee pain, worse with stairs and prolonged
standing, partially relieved by acetaminophen. He denies chest pain, denies
shortness of breath, and denies any fever or chills. There is no evidence of
an acute infection. He continues to smoke approximately half a pack of
cigarettes per day and has done so for thirty years.

PAST MEDICAL HISTORY:
Type 2 diabetes mellitus. Essential hypertension. Hyperlipidemia.

FAMILY HISTORY:
Father with obstructive sleep apnea. Mother deceased, stroke.

SOCIAL HISTORY:
Lives with spouse. Works as a warehouse supervisor. Current smoker.

MEDICATIONS:
Metformin 1000 mg PO BID
Lisinopril 20 mg PO daily
Atorvastatin 40 mg PO daily

ALLERGIES:
No known drug allergies.

REVIEW OF SYSTEMS:
Constitutional: negative for fever. Cardiovascular: negative for chest pain.
Respiratory: negative for cough. Neurologic: positive for bilateral foot
numbness.

VITAL SIGNS:
BP 142/88   HR 78   Temp 98.2 F   Wt 214 lb   BMI 31.6

PHYSICAL EXAMINATION:
General: well-appearing, no acute distress.
Cardiovascular: regular rate and rhythm, no murmur.
Respiratory: clear to auscultation bilaterally.
Extremities: right knee with medial joint line tenderness and a small effusion.
Crepitus with range of motion. No warmth or erythema.
Neurologic: decreased monofilament sensation bilaterally to the mid-foot.

LABORATORY:
Reviewed labs from 03/10/2026. Hemoglobin A1c 8.9%. Creatinine 1.0.
Ordered a comprehensive metabolic panel and a hemoglobin A1c for three months.

ASSESSMENT AND PLAN:

1. Type 2 diabetes mellitus - poorly controlled on current therapy with an A1c
   of 8.9%, up from 7.8%. Increased metformin is not an option at maximum dose.
   Will start empagliflozin 10 mg daily. Reviewed hypoglycemia precautions.

2. Diabetic neuropathy - symptoms have progressed and now interfere with sleep.
   Start gabapentin 300 mg at bedtime, titrate as tolerated. Foot care
   reviewed in detail.

3. Essential hypertension - blood pressure above goal today at 142/88. Continue
   lisinopril 20 mg daily and recheck in six weeks.

4. Knee osteoarthritis - imaging from last year showed medial compartment
   narrowing. Arthrocentesis knee with corticosteroid injection performed today
   in the right knee. See procedure note below.

5. Nicotine dependence - counseled on cessation. Discussed nicotine replacement
   and set a quit date of 04/01/2026. Approximately 6 minutes spent on
   cessation counseling.

PROCEDURE:
Arthrocentesis knee, right. After informed consent and sterile prep, the
right knee was entered via a lateral approach. 8 mL of clear yellow fluid was
aspirated. Methylprednisolone acetate 40 mg was then injected. The patient
tolerated the procedure well. No complications.

Total encounter time including the procedure: 40 minutes.

Electronically signed by Alan T. Whitfield, MD on 03/14/2026 at 16:22.
"""

INPATIENT_NOTE = """SAINT AUGUSTA REGIONAL MEDICAL CENTER
Department of Internal Medicine

PROGRESS NOTE - HOSPITAL DAY 3

MRN: SA-2210984
Age: 74    Sex: Female
Date of Service: 02/09/2026
Attending: Priya Raghunathan, MD    NPI: 1487302956

SUBJECTIVE:
Patient admitted three days ago with progressive dyspnea and lower extremity
swelling. She reports her breathing is somewhat improved this morning but she
remains short of breath with minimal exertion. She denies chest pain. She
denies fever.

PAST MEDICAL HISTORY:
Congestive heart failure. Chronic obstructive pulmonary disease. Chronic kidney
disease stage 3. Hypertension.

OBJECTIVE:
BP 118/64   HR 92   RR 22   Temp 98.6 F   SpO2 91% on 2 L nasal cannula
General: elderly female, mild respiratory distress.
Cardiovascular: JVD to the angle of the jaw. 2+ pitting pedal edema.
Respiratory: diffuse expiratory wheezing with prolonged expiratory phase.
Extremities: warm, no calf tenderness.

LABORATORY:
Hemoglobin 8.9 g/dL, down from 10.2 on admission. MCV 78. Ferritin 14.
Creatinine 1.9. eGFR 32. BNP 1840. Potassium 3.2.
Echocardiogram 02/07/2026: ejection fraction of 35 percent with global
hypokinesis.

Reviewed the chest x-ray from admission and discussed with cardiology.

ASSESSMENT AND PLAN:

1. Congestive heart failure - volume overloaded on exam with an EF of 35
   percent. Continue furosemide 40 mg IV twice daily. Strict intake and output.
   Daily weights.

2. Chronic obstructive pulmonary disease - increased sputum production and
   wheezing since admission, consistent with an acute exacerbation. Continue
   nebulizer treatment every 6 hours and prednisone taper.

3. Chronic kidney disease stage 3 - creatinine up from baseline 1.5 in the
   setting of diuresis. eGFR 32. Monitor closely.

4. Anemia - hemoglobin has fallen to 8.9. Ferritin is low at 14. No overt
   bleeding. Will obtain iron studies and guaiac stools.

5. Hypokalemia - potassium 3.2. Replace with 40 mEq oral potassium chloride.

Chest x-ray 2 views ordered for this morning to reassess.
Electrocardiogram routine 12-lead with interpretation and report performed,
showing sinus tachycardia without acute ischemic change.

Continue current level of care. Anticipate two to three more hospital days.
"""

PROCEDURE_NOTE = """NORTHGATE ORTHOPEDIC ASSOCIATES

PROCEDURE NOTE

Patient ID: NG-551029
Age: 66    Sex: Female
Date of Service: 05/22/2026
Place of Service: 11
Provider: Marcus D. Elleby, MD    NPI: 1275639840

INDICATION:
Left knee osteoarthritis with persistent effusion and pain refractory to oral
analgesics and physical therapy.

PROCEDURE PERFORMED:
Knee injection with ultrasound guidance, left knee.
Ultrasound guidance needle placement was used throughout.

DESCRIPTION OF PROCEDURE:
After informed consent was obtained, the left knee was prepped in the usual
sterile fashion. Under direct ultrasound visualization, a 21-gauge needle was
advanced into the suprapatellar recess. 12 mL of straw-colored fluid was
aspirated. Triamcinolone acetonide 40 mg was injected without difficulty.
The needle was withdrawn and a dressing applied. The patient tolerated the
procedure well and there were no immediate complications.

ASSESSMENT:
Knee osteoarthritis, left, with effusion.

PLAN:
Ice as needed. Follow up in six weeks. Consider viscosupplementation if
symptoms recur.

Electronically signed by Marcus D. Elleby, MD on 05/22/2026.
"""


_BOLD = {"Helvetica": "Helvetica-Bold", "Times-Roman": "Times-Bold",
         "Courier": "Courier-Bold"}


def write_pdf(path: Path, body: str, *, font: str = "Helvetica",
              size: int = 9.5, leading: float = 12.6) -> None:
    c = canvas.Canvas(str(path), pagesize=LETTER)
    width, height = LETTER
    margin = 0.85 * inch
    y = height - margin
    for raw in body.splitlines():
        if y < margin:
            c.showPage()
            y = height - margin
        line = raw
        # Bold the section headers so the layout reads like a real note.
        if line.strip() and line.strip() == line.strip().upper() and len(line.strip()) > 3:
            c.setFont(_BOLD.get(font, "Helvetica-Bold"), size)
        else:
            c.setFont(font, size)
        c.drawString(margin, y, line[:110])
        y -= leading
    c.save()


def scanify(src: Path, dest: Path, *, dpi: int = 200, seed: int = 7) -> None:
    """Turn a digital PDF into an image-only PDF that looks like a fax."""
    from pdf2image import convert_from_path
    from PIL import Image, ImageDraw, ImageFilter

    random.seed(seed)
    pages = convert_from_path(str(src), dpi=dpi)
    processed = []
    for i, page in enumerate(pages):
        img = page.convert("L")
        # Slight rotation, as if the page were fed crooked.
        img = img.rotate(random.uniform(-0.9, 0.9), resample=Image.BICUBIC,
                         fillcolor=255, expand=False)
        # Speckle noise.
        draw = ImageDraw.Draw(img)
        w, h = img.size
        for _ in range(int(w * h * 0.00012)):
            x, y = random.randrange(w), random.randrange(h)
            draw.point((x, y), fill=random.randrange(60, 160))
        # Soften, then re-threshold: the classic fax look.
        img = img.filter(ImageFilter.GaussianBlur(0.4))
        img = img.point(lambda p: 255 if p > 168 else 0).convert("L")
        processed.append(img)
    first, rest = processed[0], processed[1:]
    first.save(str(dest), save_all=True, append_images=rest, resolution=float(dpi))


def main() -> None:
    office = HERE / "01_office_visit_digital.pdf"
    inpatient_src = HERE / "_inpatient_source.pdf"
    inpatient = HERE / "02_inpatient_scanned.pdf"
    procedure = HERE / "03_procedure_note_digital.pdf"

    write_pdf(office, OFFICE_NOTE)
    write_pdf(procedure, PROCEDURE_NOTE)
    write_pdf(inpatient_src, INPATIENT_NOTE, font="Times-Roman", size=10, leading=13.2)
    scanify(inpatient_src, inpatient)
    inpatient_src.unlink(missing_ok=True)

    for p in (office, inpatient, procedure):
        print(f"wrote {p.name}  ({p.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
