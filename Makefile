.PHONY: install samples run test vendor clean docker

PY ?= python3
PDFJS_VERSION ?= 3.11.174
PDFJS_CDN = https://cdnjs.cloudflare.com/ajax/libs/pdf.js/$(PDFJS_VERSION)

install:
	$(PY) -m pip install -r requirements.txt
	@echo
	@echo "System packages still required:"
	@echo "  Debian/Ubuntu : sudo apt-get install -y tesseract-ocr poppler-utils"
	@echo "  macOS         : brew install tesseract poppler"

samples:
	$(PY) samples/generate_samples.py

run:
	cd backend && $(PY) -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

test:
	$(PY) -m pytest

# Vendor pdf.js so the browser never calls a third-party CDN from a page that
# is displaying PHI. Run once, then commit frontend/vendor/.
vendor:
	mkdir -p frontend/vendor
	curl -fsSL $(PDFJS_CDN)/pdf.min.js        -o frontend/vendor/pdf.min.js
	curl -fsSL $(PDFJS_CDN)/pdf.worker.min.js -o frontend/vendor/pdf.worker.min.js
	@echo "pdf.js $(PDFJS_VERSION) vendored."

docker:
	docker build -t codeassist .
	docker run --rm -p 8000:8000 codeassist

clean:
	rm -f codeassist.db
	rm -rf storage .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
