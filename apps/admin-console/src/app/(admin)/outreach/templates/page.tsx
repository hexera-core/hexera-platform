import { all, get } from "@/lib/outreach/db";
import { extractTags, KNOWN_TAGS } from "@/lib/outreach/mail/render";
import { previewTemplateForContact } from "@/lib/outreach/engine/sender";
import { Panel, PageHeader, Chip, Empty, Field } from "@/app/_components/outreach/ui";
import { ActionForm, SubmitButton } from "@/app/_components/outreach/action-form";
import { updateTemplate } from "@/app/(admin)/outreach/actions";
import type { Contact, Template } from "@/lib/outreach/core/types";

export const dynamic = "force-dynamic";

export default async function TemplatesPage() {
  const templates = await all<Template>(`SELECT * FROM templates ORDER BY kind DESC, name`);

  // The sequence lives here rather than on its own page: what a message says
  // and when it goes are the same decision, and splitting them meant neither
  // page could show you the whole thing.
  const steps = await all<{
    campaign_name: string; step_number: number; delay_days: number;
    template_name: string; only_if_no_reply: number;
  }>(
    `SELECT ca.name AS campaign_name, s.step_number, s.delay_days, s.only_if_no_reply,
            t.name AS template_name
     FROM sequence_steps s
     JOIN templates t ON t.id = s.template_id
     JOIN campaigns ca ON ca.id = s.campaign_id
     ORDER BY ca.name, s.step_number`,
  );

  // Preview against a real contact so merge tags are exercised with real data
  // rather than placeholder text that always resolves.
  const sample = await get<Contact>(
    `SELECT * FROM contacts
     WHERE name_quality = 'person' AND email_normalized IS NOT NULL
     ORDER BY CASE priority WHEN 'HOT' THEN 0 ELSE 1 END, id LIMIT 1`,
  );

  // Resolved up front, because a preview is a database read and JSX cannot await. One query per
  // template is fine at this table's size, and Promise.all keeps them concurrent.
  const previews = await Promise.all(
    templates.map(async (template) => ({
      preview: sample ? await previewTemplateForContact(template, sample) : null,
      template,
    })),
  );

  return (
    <>
      <PageHeader
        title="Templates"
        description="Merge tags resolve against the contact record. A tag with no value is a hard error rather than a blank, so the message is held back instead of going out with a gap in it."
      />

      <Panel
        title="The sequence"
        subtitle="The order these go out in, and the gap between them. Set under Settings, on the campaign."
      >
        <ol className="list-none m-0 p-0 flex flex-col gap-2">
          {steps.map((step) => (
            <li key={`${step.campaign_name}-${step.step_number}`} className="flex items-baseline gap-3 text-[0.8125rem]">
              <span className="chip chip-accent shrink-0">step {step.step_number}</span>
              <span className="min-w-0">
                <span className="truncate block">{step.template_name}</span>
                <span className="text-[0.6875rem] text-[var(--color-faint)]">
                  {step.delay_days === 0 ? "sent at enrollment" : `+${step.delay_days} sending days`}
                  {step.only_if_no_reply ? " · only if there has been no reply" : ""}
                </span>
              </span>
            </li>
          ))}
          {!steps.length && <li><Empty>No steps configured.</Empty></li>}
        </ol>
      </Panel>

      <div className="mt-4" />

      <Panel title="Available merge tags" subtitle="Add a fallback with a pipe, e.g. {{first_name|there}}.">
        <div className="flex flex-wrap gap-1.5">
          {KNOWN_TAGS.map((tag) => (
            <code key={tag} className="chip chip-neutral">{`{{${tag}}}`}</code>
          ))}
        </div>
      </Panel>

      <div className="flex flex-col gap-4 mt-4">
        {/* Previews are queries, so they are resolved BEFORE the map: a JSX callback cannot
            await, and rendering a promise shows "[object Promise]". */}
        {previews.map(({ preview, template }) => {
          const tags = extractTags(`${template.subject} ${template.body}`);
          const unknown = tags.filter((t) => !KNOWN_TAGS.includes(t.name as (typeof KNOWN_TAGS)[number]));

          return (
            <Panel
              key={template.id}
              title={template.name}
              subtitle={template.notes ?? undefined}
              actions={<Chip value={template.kind === "opener" ? "active" : "completed"} title={template.kind} />}
            >
              <div className="grid gap-4" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(320px, 100%), 1fr))" }}>
                <ActionForm action={updateTemplate} className="flex flex-col gap-3">
                  <input type="hidden" name="template_id" value={template.id} />

                  <Field
                    label="Subject"
                    hint={
                      template.kind === "follow_up"
                        ? "Follow-ups inherit the opener subject so Gmail keeps the thread together, so this field is ignored."
                        : undefined
                    }
                  >
                    <input
                      name="subject"
                      defaultValue={template.subject}
                      className="input"
                      disabled={template.kind === "follow_up"}
                    />
                  </Field>

                  <Field label="Body">
                    <textarea name="body" defaultValue={template.body} rows={16} className="textarea" />
                  </Field>

                  {unknown.length > 0 && (
                    <p className="text-[0.75rem] text-[var(--color-crimson-soft)] m-0">
                      Unknown tag{unknown.length > 1 ? "s" : ""}:{" "}
                      {unknown.map((t) => `{{${t.name}}}`).join(", ")}. These fail at send time.
                    </p>
                  )}

                  <div><SubmitButton variant="primary">Save template</SubmitButton></div>
                </ActionForm>

                <div>
                  <div className="t-label mb-2">
                    Preview {sample ? `· ${sample.full_name}, ${sample.company}` : ""}
                  </div>
                  {preview ? (
                    preview.error ? (
                      <p className="text-[0.8125rem] text-[var(--color-crimson-soft)]">{preview.error}</p>
                    ) : (
                      <div className="panel p-3 bg-[var(--color-bg-2)]">
                        <div className="t-mono text-[0.6875rem] text-[var(--color-faint)] mb-2 pb-2 border-b border-[var(--color-line)]">
                          To: {preview.to}
                          <br />
                          Subject:{" "}
                          {template.kind === "follow_up" ? "Re: (inherits the opener's subject)" : preview.subject}
                        </div>
                        <pre className="text-[0.75rem] leading-relaxed whitespace-pre-wrap m-0 text-[var(--color-muted)] font-[family-name:var(--font-mono)] max-h-96 overflow-y-auto scroll-y">
                          {preview.body}
                        </pre>
                      </div>
                    )
                  ) : (
                    <Empty>No contact available to preview against.</Empty>
                  )}

                  {tags.length > 0 && (
                    <div className="mt-3">
                      <div className="t-label mb-1.5">Tags used</div>
                      <div className="flex flex-wrap gap-1.5">
                        {tags.map((t) => (
                          <span
                            key={t.name}
                            className={`chip ${t.hasFallback ? "chip-steel" : "chip-neutral"}`}
                            title={t.hasFallback ? "Has a fallback value" : "Required. The send fails if the contact has no value."}
                          >
                            {t.name}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </Panel>
          );
        })}

        {!templates.length && (
          <Panel title="No templates">
            <Empty>Run <code className="t-mono">npm run seed</code> to create the starter templates.</Empty>
          </Panel>
        )}
      </div>
    </>
  );
}
