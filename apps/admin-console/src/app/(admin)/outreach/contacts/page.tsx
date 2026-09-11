import { redirect } from "next/navigation";


// NEVER PRERENDERED. These pages read the outreach database, which does not exist at build time -
// and a cached contact list or queue is the answer to "what was true when this was built", which
// is never the question anyone is asking of them.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

/**
 * Contacts merged into the Partners page as its People view. This stub keeps
 * every old link and bookmark working rather than greeting them with a 404.
 */
export default function ContactsRedirect() {
  redirect("/outreach/partners?view=people");
}
