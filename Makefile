.PHONY: run test notebook paper clean

run:
	python src/run_experiment.py

test:
	pytest -q

notebook:
	jupyter nbconvert --to notebook --execute notebooks/fingerprint_feature_extraction_reconstructed.ipynb --output fingerprint_feature_extraction_reconstructed.ipynb --ExecutePreprocessor.timeout=180

paper:
	cd paper && latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex

clean:
	cd paper && latexmk -C || true
