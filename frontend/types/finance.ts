export type TransactionType = "income" | "expense";
export type DataSource = "manual" | "csv_import" | "open_finance_future";
export type GoalStatus = "green" | "yellow" | "red";
export type BudgetStatus = "ok" | "attention" | "over";

export type User = {
  id: string;
  email: string;
  name: string;
  avatar_url?: string | null;
  send_monthly_summary: boolean;
  is_active: boolean;
};

export type Settings = {
  monthly_income: number;
  daily_goal: number;
  reserve_amount: number;
  reserve_goal_amount?: number;
  reserve_current_amount?: number;
  currency: string;
};

export type Category = {
  id: number;
  name: string;
  type: TransactionType;
  color: string;
  icon: string;
  is_active?: boolean;
};

export type Transaction = {
  id: number;
  title: string;
  amount: number;
  type: TransactionType;
  category_id?: number | null;
  category_name?: string | null;
  category_color?: string | null;
  payment_method: string;
  transaction_date: string;
  notes?: string;
  card_id?: number | null;
  card_name?: string | null;
  billing_month?: string | null;
  installment_number?: number | null;
  total_installments?: number | null;
  source: DataSource;
  account?: string | null;
};

export type Card = {
  id: number;
  name: string;
  brand: string;
  last_four: string;
  credit_limit: number;
  invoice: number;
  color?: string;
  closing_day?: number;
  due_day?: number;
  availableCredit?: number;
  available_credit?: number;
  committedLimit?: number;
  committed_limit?: number;
  remainingInstallments?: number;
  invoiceAlert?: boolean;
  activeInstallments?: Array<{
    title: string;
    amount: number;
    remaining: number;
    installmentLabel?: string;
  }>;
};

export type Dashboard = {
  month: string;
  monthlyIncome: number;
  salaryBase: number;
  extraIncome: number;
  inflow: number;
  outflow: number;
  balance: number;
  projectedBalance: number;
  salaryCommittedPercent: number;
  availableToday: number;
  rhythmStatus: GoalStatus;
  closingProjection: number;
  reserve: {
    monthlyPlanned: number;
    goalAmount: number;
    currentAmount: number;
  };
  previousMonthComparison: {
    month: string;
    inflow: number;
    outflow: number;
    balance: number;
    balanceDelta: number;
    outflowDelta: number;
  };
  categoryBreakdown: Array<{ name: string | null; color: string | null; total: number }>;
  paymentMethodBreakdown: Array<{ payment_method: string; total: number }>;
  cardInvoices: Card[];
  monthlyTrend: Array<{ month: string; label: string; inflow: number; outflow: number; net: number }>;
  recentTransactions: Transaction[];
};

export type GoalDay = {
  day: number;
  spent: number;
  income: number;
  expense: number;
  net: number;
  dailyGoalDelta: number;
  remaining: number;
  progress: number;
  status: "ok" | "over" | "empty";
};

export type Goal = {
  month: string;
  dailyGoal: number;
  reserveAmount: number;
  availableBudget: number;
  recommendedDailyGoal: number;
  targetDailyGoal: number;
  allowedRemaining: number;
  daysAboveGoal: number;
  daysBelowGoal: number;
  projectedClosing: number;
  currentAverageSpend: number;
  goalStatus: GoalStatus;
  riskAlert: string;
  progressDay: number;
  totalDays: number;
  days: GoalDay[];
};

export type BudgetItem = {
  id: number;
  categoryId: number;
  categoryName: string;
  categoryColor: string;
  categoryIcon: string;
  plannedAmount: number;
  spent: number;
  remaining: number;
  progress: number;
  status: BudgetStatus;
};

export type BudgetSummary = {
  month: string;
  totalPlanned: number;
  totalSpent: number;
  remaining: number;
  items: BudgetItem[];
  unbudgetedCategories: Array<{
    categoryId: number;
    categoryName: string;
    categoryColor: string;
    categoryIcon: string;
    spent: number;
  }>;
};

export type Alert = {
  type: "info" | "warning" | "danger" | string;
  category: string;
  message: string;
};

export type Score = {
  score: number;
  label: string;
  color: string;
  reason?: string;
  breakdown: Record<string, number>;
};

export type ReportSummary = {
  month: string;
  dashboard: Dashboard;
  budget: BudgetSummary;
  cards: Card[];
  score: Score;
  goals: Goal;
  paymentMethods: Array<{ payment_method: string; type: TransactionType; total: number }>;
  categoryGrowth: {
    month: string;
    previousMonth: string;
    hasHistory: boolean;
    items: Array<{
      name: string;
      color: string;
      currentTotal: number;
      previousTotal: number;
      delta: number;
      percentChange: number | null;
    }>;
  };
  alerts: Alert[];
};

export type CsvUpload = {
  importToken: string;
  filename: string;
  columns: string[];
  totalRows: number;
  preview: Array<Record<string, string>>;
};

export type CsvPreviewRow = {
  line: number;
  transactionDate: string;
  detectedMonth?: string;
  monthLabel?: string;
  year?: number;
  monthNumber?: number;
  day?: number;
  time?: string | null;
  title: string;
  rawDescription?: string;
  amount: number;
  type: TransactionType;
  categoryId?: number | null;
  categoryName?: string | null;
  account?: string | null;
  duplicateHash: string;
  legacyDuplicateHash?: string;
};

export type CsvImportMode = "merge" | "replace";

export type CsvPreview = {
  importToken: string;
  columns: string[];
  totalRows: number;
  validRows: number;
  invalidRows: number;
  duplicateRows: number;
  duplicates: CsvPreviewRow[];
  preview: CsvPreviewRow[];
  months: Array<{ month: string; label: string }>;
  totalAmount: number;
  /** Lançamentos que já existem nos meses do arquivo — o que "substituir" apaga. */
  existingInMonths: number;
  errors: Array<{ line: number; detail: string }>;
};

export type Bootstrap = {
  settings: Settings;
  categories: Category[];
  cards: Card[];
  transactions: Transaction[];
  dashboard: Dashboard;
  budget: BudgetSummary;
  score: Score;
  previousScore?: Score;
  alerts: Alert[];
  user: User;
};
