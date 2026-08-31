# PDF and OfficeQA evaluation

PDF and OfficeQA targets are eval-only Harbor adapters. They exercise document reading and structured extraction in a task-provided environment.

```bash
harbor run -d officeqa@1.0 \
  --agent garuda.eval.harbor_adapter:GarudaHarborAgent \
  --model openrouter/deepseek/deepseek-v4-flash-0731
harbor run -c garuda/eval/benchmarks/pdf/job.yaml
```

Use a vision-capable model or task artifacts with extracted text when PDFs are not plain text. Enable `image_read` in a custom profile when a task exposes screenshots.
