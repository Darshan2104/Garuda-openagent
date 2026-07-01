# PDF / OfficeQA eval (Harbor)

Eval-only adapter notes for document QA benchmarks (e.g. OfficeQA). Not part of core Garuda.

## Overview

PDF eval tasks typically require reading documents and answering questions or extracting structured data. Garuda's `read_file` and `bash` tools operate inside the Harbor task container; multimodal PDF parsing depends on task-provided utilities.

## Run

When a Harbor dataset is available:

```bash
harbor run -d officeqa@1.0 \
  --agent garuda.eval.harbor_adapter:GarudaHarborAgent \
  --model openai/gpt-4o-mini
```

## Job config

```bash
harbor run -c garuda/eval/benchmarks/pdf/job.yaml
```

## Tips

- Use models with vision or task images that include pre-extracted text when PDFs are not plain text.
- Enable `image_read` in a custom agent profile if trials expose screenshot artifacts.
- Check `agent/trajectory.json` for ATIF export after each trial.
