# History capacity ablation

**Superseded before execution.** Automatic approval rejected this additional variant; it made zero API calls. The user subsequently requested a product change to 320,000 characters with AI compaction at 160,000. The original 80,000-character AIPod round was stopped on that instruction at 179 reserved requests. Follow-up testing uses the implemented default compaction policy, not the unexecuted capacity-only ablation described below.

Declared after observing the primary default AIPod run, before this diagnostic makes any model call. This is a follow-up diagnosis and is not an independent replication of a preregistered superiority claim.

In the primary identity round, the AIPod worker had not created or changed application files at reserved HTTP request 95. Its native history policy trims serialized history above 80,000 characters: the earliest retained reply moves from MIGRATION.md at request 68 to beans_config.json at request 95. Native mini-SWE-agent retains its full conversation. The primary run is not interrupted or altered.

Run the same identity requirement once more from the original migrated baseline, with the same model, temperature, native prompts, translator, ownership, checks and 180-request round cap. Change only the history cutoff from 80,000 to 320,000 characters, by replacing one constant in the in-memory WorkspaceAgent.run implementation. Do not modify installed framework files, the main repository, or any primary-trial application. Save the one-line patch with the report. The filesystem path differs for isolation.

The extra requests use the same atomic 1,620-request authorization ledger and include the earlier 185 invalidated requests. Both mini primary arms have finished using 232 calls total. Therefore even all 540 primary AIPod calls plus all 180 diagnostic calls yield at most 1,137 total reserved requests, within the existing authorization.

Evaluate the identical hidden round-one cases and original regression after the diagnostic ends. Report first application mutation, delivery/structure/behavioral outcomes, token breakdown and time. Success would support investigating history management; it would not by itself prove causality or broad superiority because model sampling is not deterministic and this is one additional run. Failure would show that increasing capacity alone was insufficient in this trial.
