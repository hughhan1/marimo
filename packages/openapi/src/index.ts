export type * from "./api";

import type { components as notebook } from "./notebook";
import type { components as session } from "./session";

export type Session = session["schemas"];
export type Notebook = notebook["schemas"];
