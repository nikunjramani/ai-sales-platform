import {
  ChevronRight,
  FilePen,
  FileCheck,
  MessageCircle,
  PartyPopper,
  Rocket,
  TriangleAlert,
  UserRoundSearch,
  type LucideIcon,
} from "lucide-react";
import { Link } from "react-router";

import { messagePreview } from "@/features/conversations";
import { dateTime } from "@/shared/lib/format";
import { QueryState } from "@/shared/ui/QueryState";

import { useHomeSummary } from "../../hooks/useHomeSummary";   
import { allDone, countText, greeting, type HomeCard, type HomeCardId } from "../../services/homeCards";

const CARD_ICONS: Record<HomeCardId, { icon: LucideIcon; tone: string }> = {
  approve: { icon: FileCheck, tone: "info" },
  changes: { icon: FilePen, tone: "warn" },
  requests: { icon: UserRoundSearch, tone: "warn" },
  orders: { icon: TriangleAlert, tone: "bad" },
};

/** The first page after sign-in: what needs this person now, each with one big "Open" button. */
export function HomePage() {
  const summary = useHomeSummary();
  const hello = greeting(new Date().getHours());
  return (
    <section>
      <header className="home-hello">
        <h1>
          {hello}
          {summary.name ? `, ${summary.name}` :""}!
        </h1>
        <p className="hint">Here is what needs you today at {summary.businessName}.</p>
      </header>

      {summary.setup && (
        <Link to="/setup" className="todo-card tone-info">
          <span className="todo-icon" aria-hidden="true">
            <Rocket size={28} />
          </span>
          <span className="todo-text">
            <strong>Get started</strong>
            <span className="progress" aria-hidden="true">
              <span style={{ width: `${(100 * summary.setup.done) / Math.max(summary.setup.total, 1)}%` }} />
            </span>
            <span className="muted small">
              {summary.setup.done} of {summary.setup.total} steps done
            </span>
          </span>
          <ChevronRight size={24} aria-hidden="true" />
        </Link>
      )}

      {allDone(summary.cards) && (
        <div className="all-done">
          <PartyPopper size={40} aria-hidden="true" />
          <strong>All done!</strong>
          <span className="muted">Nothing needs you right now.</span>
        </div>
      )}

      <ul className="todo-grid" aria-label="Things to do">
        {summary.cards.map((card) => (
          <li key={card.id}>
            <TodoCard card={card} />
          </li>
        ))}
      </ul>

      {summary.messages && (
        <section className="card">
          <div className="title-row">
            <h2>
              <MessageCircle size={20} aria-hidden="true" /> Latest messages
            </h2>
            <Link className="link" to="/inbox">
              See all
            </Link>
          </div>
          <QueryState
            isLoading={summary.messages.query.isLoading}
            error={summary.messages.query.error}
            isEmpty={summary.messages.list?.length === 0}
            emptyText="No messages yet."
          >
            <ul className="list">
              {summary.messages.list?.map((item) => (
                <li key={item.conversation_id}>
                  <Link className="row" to={`/inbox/${item.conversation_id}`}>
                    <span>
                      <strong>{item.contact_name ?? "Unknown customer"}</strong>
                      <br />
                      <span className="quote">
                        {item.last_direction === "outbound" ? "You: " : ""}
                        {messagePreview(item.last_text, 80)}
                      </span>
                    </span>
                    <span className="muted small">{dateTime(item.last_at)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </QueryState>
        </section>
      )}
    </section>
  );
}

function TodoCard({ card }: { card: HomeCard }) {
  const { icon: Icon, tone } = CARD_ICONS[card.id];
  const zero = card.count.count === 0;
  return (
    <Link to={card.link} className={`todo-card ${zero ? "tone-done" : `tone-${tone}`}`}>
      <span className="todo-icon" aria-hidden="true">
        <Icon size={28} />
      </span>
      <span className="todo-text">
        <span className="todo-count">{countText(card.count)}</span>
        <span>{card.title}</span>
        {card.count.failed && <span className="error small">Could not load. Open the list to see.</span>}
      </span>
      <span className="todo-open">
        Open <ChevronRight size={20} aria-hidden="true" />
      </span>
    </Link>
  );
}
