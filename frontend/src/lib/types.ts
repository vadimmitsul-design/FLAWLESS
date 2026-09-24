export type Customer = {
  id: number;
  name: string;
  email: string;
  is_child: boolean;
};
export type ApiKey = {
  id: number;
  name: string;
  prefix: string;
  created_at: string;
  daily_limit_rub: string | null;
  monthly_limit_rub: string | null;
};
export type Usage = {
  id: string;
  created_at: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  charged_rub: string;
  status: string;
};
export type Conversation = {
  id: number;
  title: string;
  model: string;
  updated_at: string;
};
export type ChatMessage = {
  role: string;
  text: string;
  attachment_name: string | null;
};
export type Dashboard = {
  customer: Customer;
  wallet: {
    balance_rub: string;
    reserved_rub: string;
    available_rub: string;
    spent_today_rub: string;
    spent_month_rub: string;
    daily_limit_rub: string | null;
    monthly_limit_rub: string | null;
  };
  stats: { requests_month: number; tokens_month: number };
  models: string[];
  usage: Usage[];
  daily_usage: {
    date: string;
    charged_rub: string;
    requests: number;
    tokens: number;
  }[];
  api_keys: ApiKey[];
  topups: {
    id: number;
    amount_rub: string;
    status: string;
    note: string;
    created_at: string;
  }[];
  conversations: Conversation[];
  flags: {
    resources: boolean;
    shop: boolean;
    prompts: boolean;
    children: boolean;
    archive: boolean;
    telegram: boolean;
  };
};
export type View =
  "overview" | "chat" | "models" | "usage" | "keys" | "wallet" | "settings";
