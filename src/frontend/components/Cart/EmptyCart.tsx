// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0

import Link from 'next/link';
import Button from '../Button';
import * as S from '../../styles/Cart.styled';

const EmptyCart = () => {
  return (
    <S.EmptyCartContainer>
      <S.Title>Your cart is empty... for now.</S.Title>
      <S.Subtitle>Everything you add will float right here.</S.Subtitle>

      <S.ButtonContainer>
        <Link href="/">
          <Button type="submit">Continue Shopping</Button>
        </Link>
      </S.ButtonContainer>
    </S.EmptyCartContainer>
  );
};

export default EmptyCart;
