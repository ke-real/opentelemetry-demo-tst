// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0

import { DefaultTheme } from 'styled-components';

const Theme: DefaultTheme = {
  colors: {
    otelBlue: '#E3121B',
    otelYellow: '#B80F17',
    otelGray: '#03070F',
    otelRed: '#FF3B42',
    backgroundGray: 'rgba(255, 255, 255, 0.04)',
    lightBorderGray: 'rgba(255, 255, 255, 0.14)',
    borderGray: '#2B3548',
    textGray: '#EEF1F6',
    textLightGray: '#9AA4B6',
    white: '#FFFFFF',
    background: '#070C16',
    surface: '#101828',
  },
  breakpoints: {
    desktop: '@media (min-width: 768px)',
  },
  sizes: {
    mxLarge: '22px',
    mLarge: '20px',
    mMedium: '14px',
    mSmall: '12px',
    dxLarge: '58px',
    dLarge: '40px',
    dMedium: '18px',
    dSmall: '16px',
    nano: '8px',
  },
  fonts: {
    bold: '800',
    regular: '500',
    semiBold: '700',
    light: '400',
  },
};

export default Theme;
