# Claims extraction schema 1.1: worked examples

Four article-level extraction records illustrate the required scientific objects, local results, result families, source links, and numerical-table exports. The records use `schema_version: "1.1"` and independently allocated extraction ID `E002`.

## Records and coverage

| Paper | JSON record | Result families | Local results | Table CSVs | Source coverage |
|---|---|---:|---:|---:|---|
| Shukla et al. | [Shukla_Extraction_v1.1.json](Shukla_Extraction_v1.1.json) | 1 | 14 | 1 | Complete for the supplied two-page article; no supplement identified. |
| Genther et al. | [Genther_Extraction_v1.1.json](Genther_Extraction_v1.1.json) | 1 | 33 | 2 | Complete for the supplied article; no supplement identified. |
| Ohtsuki et al. | [Ohtsuki_Extraction_v1.1.json](Ohtsuki_Extraction_v1.1.json) | 5 | 81 | 0 | Published article and recovered 11-page Supplementary Information reviewed. Neither contains a qualifying numerical source table. |
| Perel et al. | [Perel_Extraction_v1.1.json](Perel_Extraction_v1.1.json) | 6 | 56 | 2 | Supplied six-page article extracted; external study-level quality table unavailable. `table_coverage.status` is `incomplete`. |

Source papers (paper key → DOI):

| Key | Paper | Source |
|---|---|---|
| P001 | Shukla et al., *Diabetes Care* | [10.2337/dc15-0429](https://doi.org/10.2337/dc15-0429) |
| P002 | Genther et al., *JAMA* | [10.1001/jama.2013.5912](https://doi.org/10.1001/jama.2013.5912) |
| P003 | Ohtsuki et al., *Nature* | [10.1038/nature04605](https://doi.org/10.1038/nature04605) and [Supplementary Information](https://www.cs.princeton.edu/courses/archive/fall24/cos597C/bib/nature04605-appendix.pdf) |
| P004 | Perel et al., *BMJ* | [10.1136/bmj.39048.407928.BE](https://doi.org/10.1136/bmj.39048.407928.BE) and [correction](https://www.bmj.com/content/334/7585/0.4) |

The source PDFs are not included in this repository; use the links above. The source documents and precise extraction scope are identified inside each JSON. Ohtsuki and Perel also contain cross-group summaries. Local result counts do not count independent studies: multiple comparisons or plotted series can share one investigation and sample.

## What each example demonstrates

- **Shukla:** all 14 paired table comparisons, shared participants and protocol, SEM versus participant SD, and explicit preservation of differing prose/table reports.
- **Genther:** descriptive sample information and all reported regression models, with adjustment details, confidence intervals, and the two complete numerical tables.
- **Ohtsuki:** model definitions, all 35 numbered supplementary equations, distinct updating mechanisms, and 60 simulation curves containing 520 approximate visible-marker coordinates. Payoff matrices are mathematical objects. No table CSV is required.
- **Perel:** six intervention-specific review families, a cross-group synthesis, source study counts, pooled estimates and uncertainty, graphical data, and a numerical table embedded within a forest plot. The missing external table is reported explicitly.

## Read the structure

1. **Start with `paper`, `extraction`, and `sources`.** Coverage is a source-level claim; it is separate from whether individual local closures are resolved.
2. **Read `fields[]` within results and shared context.** Each scientific object has its own ID, label, value, and supporting context or provenance. A field may hold a structured series. Addressed components use `parent_field_id` and `field_path` without becoming independent evidence.
3. **Use `relationships` and `closure` to interpret local results.** `closure.core_ids` identifies the local core. `member_ids` records its scientific inputs; `reference_ids` retains external context and provenance.
4. **Use `group_id` and `result_groups` for families.** A family's `core_result_ids` selects its main local records. The accompanying question, investigation, and grouping reason explain its boundary.
5. **Use `tables` to locate complete CSV exports.** Each numerical source table has one T object and one CSV. Non-closing `table_for` relationships connect the table to relevant ordinary results; scientific values reference their cells through `table_references`.

## CSV companions

Keep the CSV files beside their JSON records. Each `csv_file` equals its complete table ID plus `.csv`. CSV row numbers in `table_references` begin at 1 and exclude the header.

| File | Source region | Data rows |
|---|---|---:|
| [P001.E002.T001.csv](P001.E002.T001.csv) | Shukla Table 1 | 14 |
| [P002.E002.T001.csv](P002.E002.T001.csv) | Genther Table 1 | 36 |
| [P002.E002.T002.csv](P002.E002.T002.csv) | Genther Table 2 | 5 |
| [P004.E002.T001.csv](P004.E002.T001.csv) | Perel study-quality table | 6 |
| [P004.E002.T002.csv](P004.E002.T002.csv) | Perel Figure 1 printed numerical table | 5 |

The five CSVs contain all 66 data rows from the numerical tables available in the reviewed sources. Composite cells, signs, precision, and source missing-value markers are preserved as displayed. Header hierarchy, units, captions, and footnotes are retained in table metadata. The separately structured scientific fields provide semantic labels and numerical components without replacing the CSV transcription.

## Precision and remaining source gap

Graph readings have `origin: "digitized"` and explicit approximation notes. They represent visible plotted data, not recovered raw simulation or experiment files. Perel's clipped confidence intervals are identified, and lower confidence limits hidden inside bars remain unreported. Repeated graphical and textual representations are not independent evidence.

Perel explicitly refers to a separate detailed animal-study quality table. That source could not be retrieved. An additional unavailable supplement is identified as a bibliography, not asserted to contain another numerical table. Consequently, Perel is a complete extraction of the supplied article but is not an example of complete coverage of every associated source. Its record illustrates how to declare that limitation without inventing data.

## Validation

All four records were checked against the [JSON Schema 1.1](../schema.json) (field guide: [Quick reference](../QUICK_REFERENCE.md)), including its controlled vocabularies. Additional checks covered unique IDs, resolved references, result-family membership, closure membership and boundary references, table links, CSV filenames/dimensions, table cell coordinates, and addressed component paths. Source values and table transcriptions were checked against the supplied PDFs. These checks do not replace the explicit source-coverage and graphical-precision qualifications above.
