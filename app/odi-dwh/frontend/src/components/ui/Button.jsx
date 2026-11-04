import { clsx } from 'clsx';

const variants = {
  primary: 'btn-primary',
  ghost:   'btn-ghost',
  danger:  'btn-danger',
  icon:    'btn-icon btn-ghost',
};

export function Button({ children, variant = 'primary', className = '', disabled, onClick, title, type = 'button' }) {
  return (
    <button
      type={type}
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={clsx(variants[variant] || variants.primary, disabled && 'opacity-50 cursor-not-allowed', className)}
    >
      {children}
    </button>
  );
}
