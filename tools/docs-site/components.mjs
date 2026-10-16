// React implementations of the Mintlify components used in docs/*.mdx.
// They render to static HTML; theme/docs.js adds the small bits of interactivity
// (tabs, code-group switching, copy buttons) on top.

import { Children, createElement as h, isValidElement } from "react";

const ICONS = {
  info: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm1 15h-2v-6h2Zm0-8h-2V7h2Z",
  tip: "M9 21h6v-1H9Zm3-19a7 7 0 0 0-4 12.7V17a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1v-2.3A7 7 0 0 0 12 2Z",
  warning: "M1 21h22L12 2Zm12-3h-2v-2h2Zm0-4h-2v-4h2Z",
  danger: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm5 13.6L15.6 17 12 13.4 8.4 17 7 15.6 10.6 12 7 8.4 8.4 7 12 10.6 15.6 7 17 8.4 13.4 12Z",
  check: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm-2 15-5-5 1.4-1.4 3.6 3.6 7.6-7.6L19 8Z",
  note: "M4 4h16v12H8l-4 4Zm3 4v2h10V8Zm0 4v-0h7v0Z",
};

function icon(kind) {
  return h(
    "svg",
    { className: "callout-icon", viewBox: "0 0 24 24", "aria-hidden": "true", width: 18, height: 18 },
    h("path", { d: ICONS[kind] || ICONS.info, fill: "currentColor" }),
  );
}

function callout(kind, label) {
  const Callout = ({ children, title }) =>
    h(
      "div",
      { className: `callout callout-${kind}`, role: "note", "aria-label": title || label },
      icon(kind),
      h("div", { className: "callout-body" }, title ? h("p", { className: "callout-title" }, title) : null, children),
    );
  Callout.displayName = label;
  return Callout;
}

function elements(children) {
  return Children.toArray(children).filter(isValidElement);
}

function slug(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

// --------------------------------------------------------------------------- cards
function Card({ title, href, icon: _icon, children, horizontal }) {
  const body = [
    title ? h("p", { className: "card-title", key: "t" }, title) : null,
    children ? h("div", { className: "card-body", key: "b" }, children) : null,
  ];
  const className = `card${horizontal ? " card-horizontal" : ""}`;
  return href ? h("a", { className, href }, body) : h("div", { className }, body);
}

function CardGroup({ cols = 2, children }) {
  return h("div", { className: "card-group", style: { "--cols": cols } }, children);
}

// --------------------------------------------------------------------------- steps
function Steps({ children }) {
  return h("ol", { className: "steps" }, children);
}

function Step({ title, children }) {
  return h("li", { className: "step" }, title ? h("p", { className: "step-title" }, title) : null,
    h("div", { className: "step-body" }, children));
}

// --------------------------------------------------------------------------- tabs
// Rendered as stacked, labelled panels; docs.js upgrades them into real tabs.
function Tabs({ children }) {
  const tabs = elements(children);
  return h(
    "div",
    { className: "tabs", "data-tabs": "" },
    tabs.map((tab, index) =>
      h(
        "section",
        { className: "tab-panel", key: index, "data-title": tab.props.title || `Tab ${index + 1}` },
        h("p", { className: "tab-label" }, tab.props.title || `Tab ${index + 1}`),
        tab.props.children,
      ),
    ),
  );
}

function Tab({ children }) {
  return h("div", null, children);
}

function CodeGroup({ children }) {
  const blocks = elements(children);
  return h(
    "div",
    { className: "tabs code-group", "data-tabs": "" },
    blocks.map((block, index) => {
      const title = block.props["data-title"] || block.props.title || `Code ${index + 1}`;
      return h("section", { className: "tab-panel", key: index, "data-title": title }, block);
    }),
  );
}

// --------------------------------------------------------------------------- accordions
function Accordion({ title, children, defaultOpen }) {
  return h(
    "details",
    { className: "accordion", open: defaultOpen ? true : undefined, id: slug(title) || undefined },
    h("summary", null, title),
    h("div", { className: "accordion-body" }, children),
  );
}

function AccordionGroup({ children }) {
  return h("div", { className: "accordion-group" }, children);
}

function Expandable({ title = "Show more", children, defaultOpen }) {
  return h(Accordion, { title, defaultOpen }, children);
}

// --------------------------------------------------------------------------- misc
function Frame({ caption, children }) {
  return h("figure", { className: "frame" }, children, caption ? h("figcaption", null, caption) : null);
}

function Badge({ children, color }) {
  return h("span", { className: `badge${color ? ` badge-${color}` : ""}` }, children);
}

function Tooltip({ tip, headline, children }) {
  return h("abbr", { className: "tooltip", title: [headline, tip].filter(Boolean).join(": ") }, children);
}

function Icon() {
  return null;
}

function Field(kind) {
  const Component = ({ name, path, query, body, header, type, required, default: def, children }) => {
    const label = name || path || query || body || header;
    return h(
      "div",
      { className: `field field-${kind}` },
      h(
        "p",
        { className: "field-head" },
        h("code", null, label),
        type ? h("span", { className: "field-type" }, type) : null,
        required ? h("span", { className: "badge badge-red" }, "required") : null,
        def !== undefined ? h("span", { className: "field-default" }, `default: ${def}`) : null,
      ),
      h("div", { className: "field-body" }, children),
    );
  };
  Component.displayName = kind;
  return Component;
}

function Update({ label, description, children }) {
  return h(
    "section",
    { className: "update", id: slug(label) || undefined },
    h("p", { className: "update-label" }, label, description ? h("span", null, ` · ${description}`) : null),
    children,
  );
}

// --------------------------------------------------------------------------- html overrides
function heading(tag) {
  const Heading = ({ id, children, ...rest }) =>
    h(
      tag,
      { id, ...rest },
      children,
      id ? h("a", { className: "anchor", href: `#${id}`, "aria-label": "Link to this section" }, "#") : null,
    );
  Heading.displayName = tag;
  return Heading;
}

function Table(props) {
  return h("div", { className: "table-wrap" }, h("table", props));
}

function Pre({ children, ...props }) {
  const title = props["data-title"];
  return h(
    "div",
    { className: "code-block" },
    title ? h("p", { className: "code-title" }, title) : null,
    h("pre", props, children),
  );
}

export const components = {
  Note: callout("note", "Note"),
  Info: callout("info", "Info"),
  Tip: callout("tip", "Tip"),
  Warning: callout("warning", "Warning"),
  Danger: callout("danger", "Danger"),
  Check: callout("check", "Check"),
  Callout: callout("info", "Callout"),
  Card,
  CardGroup,
  Columns: CardGroup,
  Steps,
  Step,
  Tabs,
  Tab,
  CodeGroup,
  Accordion,
  AccordionGroup,
  Expandable,
  Frame,
  Badge,
  Tooltip,
  Icon,
  ParamField: Field("param"),
  ResponseField: Field("response"),
  Update,
  h2: heading("h2"),
  h3: heading("h3"),
  h4: heading("h4"),
  table: Table,
  pre: Pre,
};
